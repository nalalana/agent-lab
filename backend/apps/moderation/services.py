import logging
import os
import time
import base64
import hashlib
import json
import re
from typing import Any

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, is_granted
from apps.moderation.models import ImageModerationAsset, SensitiveWordRule, TextModerationAsset, TextModerationEvent
from apps.user.models import User
from common.utils.Tengcentcos_utils import (
    _get_cos_client_and_bucket,
    get_public_cdn_url,
    normalize_cos_key,
)


logger = logging.getLogger("touming")

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _now_ts() -> str:
    return str(time.time())[:20]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]


def is_image_file(storage_key: str = "", mime_type: str = "") -> bool:
    content_type = str(mime_type or "").strip().lower()
    if content_type in IMAGE_MIME_TYPES:
        return True
    return str(storage_key or "").strip().lower().endswith(IMAGE_EXTENSIONS)


class ImageModerationService:
    PROVIDER_DISABLED = "disabled"
    PROVIDER_LOCAL = "local"
    PROVIDER_COS = "cos"

    @staticmethod
    def provider() -> str:
        return os.getenv("IMAGE_MODERATION_PROVIDER", ImageModerationService.PROVIDER_LOCAL).strip().lower()

    @staticmethod
    def allow_untracked() -> bool:
        return _env_bool("IMAGE_MODERATION_ALLOW_UNTRACKED", False)

    @staticmethod
    def local_decision() -> str:
        return os.getenv(
            "IMAGE_MODERATION_LOCAL_DECISION",
            ImageModerationAsset.STATUS_APPROVED,
        ).strip().lower()

    @staticmethod
    def uses_local_auto_approve() -> bool:
        return (
            ImageModerationService.provider() == ImageModerationService.PROVIDER_LOCAL
            and ImageModerationService.local_decision() == ImageModerationAsset.STATUS_APPROVED
        )

    @staticmethod
    def is_moderation_enforced() -> bool:
        if ImageModerationService.uses_local_auto_approve():
            return False
        return ImageModerationService.provider() not in {
            ImageModerationService.PROVIDER_DISABLED,
            "",
        }

    @staticmethod
    def ensure_reviewed(asset: ImageModerationAsset) -> ImageModerationAsset:
        if ImageModerationService.uses_local_auto_approve():
            if asset.status == ImageModerationAsset.STATUS_REJECTED:
                return asset
            if asset.status != ImageModerationAsset.STATUS_APPROVED:
                return ImageModerationService.apply_result(
                    asset,
                    status=ImageModerationAsset.STATUS_APPROVED,
                    provider_result={"provider": "local", "decision": ImageModerationAsset.STATUS_APPROVED},
                )
            return asset
        if asset.status in {
            ImageModerationAsset.STATUS_APPROVED,
            ImageModerationAsset.STATUS_REJECTED,
            ImageModerationAsset.STATUS_REVIEW_REQUIRED,
        }:
            return asset
        return ImageModerationService.submit_or_apply_local_result(asset)

    @staticmethod
    def register_uploaded_image(
        storage_key: str,
        *,
        source: str,
        source_field: str = "",
        owner_type: str = "",
        owner_id: str = "",
        mime_type: str = "",
        size: int = 0,
        submit: bool = True,
    ) -> ImageModerationAsset:
        key = normalize_cos_key(storage_key)
        now = _now_ts()
        asset, _created = ImageModerationAsset.objects.update_or_create(
            storage_key=key,
            defaults={
                "source": source,
                "source_field": source_field,
                "owner_type": owner_type,
                "owner_id": owner_id,
                "mime_type": mime_type or "",
                "size": int(size or 0),
                "provider": ImageModerationService.provider(),
                "updated_at": now,
            },
        )
        if submit and (
            ImageModerationService.uses_local_auto_approve()
            or asset.status not in {
                ImageModerationAsset.STATUS_APPROVED,
                ImageModerationAsset.STATUS_REJECTED,
                ImageModerationAsset.STATUS_REVIEW_REQUIRED,
            }
        ):
            ImageModerationService.submit_or_apply_local_result(asset)
            asset.refresh_from_db()
        return asset

    @staticmethod
    def submit_or_apply_local_result(asset: ImageModerationAsset) -> ImageModerationAsset:
        provider = ImageModerationService.provider()
        if provider == ImageModerationService.PROVIDER_LOCAL:
            decision = ImageModerationService.local_decision()
            if decision not in dict(ImageModerationAsset.STATUS_CHOICES):
                decision = ImageModerationAsset.STATUS_PENDING
            return ImageModerationService.apply_result(
                asset,
                status=decision,
                provider_result={"provider": "local", "decision": decision},
            )
        if provider == ImageModerationService.PROVIDER_COS:
            return ImageModerationService.submit_cos_audit(asset)
        return ImageModerationService.apply_result(
            asset,
            status=ImageModerationAsset.STATUS_PENDING,
            provider_result={"provider": provider or ImageModerationService.PROVIDER_DISABLED},
        )

    @staticmethod
    def submit_cos_audit(asset: ImageModerationAsset) -> ImageModerationAsset:
        try:
            client, bucket_name = _get_cos_client_and_bucket()
            detect_type = os.getenv("IMAGE_MODERATION_COS_DETECT_TYPE", "Porn,Terrorism,Politics,Ads").strip()
            biz_type = os.getenv("IMAGE_MODERATION_COS_BIZ_TYPE", "").strip() or None
            callback = os.getenv("IMAGE_MODERATION_COS_CALLBACK", "").strip() or None
            freeze = os.getenv("IMAGE_MODERATION_COS_FREEZE", "").strip() or None
            async_flag = int(os.getenv("IMAGE_MODERATION_COS_ASYNC", "0").strip() or "0")
            response = client.ci_auditing_image_batch(
                Bucket=bucket_name,
                Input=[{"Object": asset.storage_key, "DataId": str(asset.id)}],
                DetectType=detect_type,
                BizType=biz_type,
                Async=async_flag,
                Callback=callback,
                Freeze=freeze,
            )
            return ImageModerationService.apply_cos_response(asset, response)
        except Exception as exc:
            logger.exception("image moderation COS submit failed storage_key=%s", asset.storage_key)
            return ImageModerationService.apply_result(
                asset,
                status=ImageModerationAsset.STATUS_FAILED,
                failure_reason=str(exc),
                provider_result={"error": str(exc)},
            )

    @staticmethod
    def refresh_cos_result(asset: ImageModerationAsset) -> ImageModerationAsset:
        if not asset.provider_job_id:
            return asset
        try:
            client, bucket_name = _get_cos_client_and_bucket()
            response = client.ci_auditing_image_query(Bucket=bucket_name, JobID=asset.provider_job_id)
            return ImageModerationService.apply_cos_response(asset, response)
        except Exception as exc:
            logger.exception("image moderation COS query failed storage_key=%s", asset.storage_key)
            asset.failure_reason = str(exc)
            asset.updated_at = _now_ts()
            asset.save(update_fields=["failure_reason", "updated_at"])
            return asset

    @staticmethod
    def apply_cos_response(asset: ImageModerationAsset, response: dict[str, Any]) -> ImageModerationAsset:
        detail = ImageModerationService._extract_cos_detail(response)
        suggestion = str(detail.get("Suggestion") or detail.get("suggestion") or "").strip()
        label = str(detail.get("Label") or detail.get("label") or "").strip()
        job_id = str(detail.get("JobId") or detail.get("JobID") or detail.get("job_id") or "").strip()
        score = ImageModerationService._extract_score(detail)
        status = ImageModerationService._status_from_suggestion(suggestion)
        return ImageModerationService.apply_result(
            asset,
            status=status,
            suggestion=suggestion,
            label=label,
            score=score,
            provider_job_id=job_id,
            provider_result=response,
        )

    @staticmethod
    def apply_result(
        asset: ImageModerationAsset,
        *,
        status: str,
        suggestion: str = "",
        label: str = "",
        score: float | None = None,
        provider_job_id: str = "",
        provider_result: dict[str, Any] | None = None,
        failure_reason: str = "",
        reviewer: str = "",
    ) -> ImageModerationAsset:
        if status not in dict(ImageModerationAsset.STATUS_CHOICES):
            status = ImageModerationAsset.STATUS_PENDING
        asset.status = status
        asset.suggestion = suggestion or asset.suggestion
        asset.label = label or asset.label
        asset.score = score if score is not None else asset.score
        asset.provider_job_id = provider_job_id or asset.provider_job_id
        asset.provider_result = provider_result if provider_result is not None else asset.provider_result
        asset.failure_reason = failure_reason or ""
        asset.provider = asset.provider or ImageModerationService.provider()
        if reviewer:
            asset.reviewed_by = reviewer
            asset.reviewed_at = _now_ts()
        asset.updated_at = _now_ts()
        asset.save()
        return asset

    @staticmethod
    def approve(storage_key: str, reviewer: str = "") -> ImageModerationAsset | None:
        asset = ImageModerationAsset.objects.filter(storage_key=normalize_cos_key(storage_key)).first()
        if not asset:
            return None
        return ImageModerationService.apply_result(
            asset,
            status=ImageModerationAsset.STATUS_APPROVED,
            reviewer=reviewer,
        )

    @staticmethod
    def reject(storage_key: str, reviewer: str = "", reason: str = "") -> ImageModerationAsset | None:
        asset = ImageModerationAsset.objects.filter(storage_key=normalize_cos_key(storage_key)).first()
        if not asset:
            return None
        return ImageModerationService.apply_result(
            asset,
            status=ImageModerationAsset.STATUS_REJECTED,
            failure_reason=reason,
            reviewer=reviewer,
        )

    @staticmethod
    def get_asset(storage_key: str) -> ImageModerationAsset | None:
        key = normalize_cos_key(storage_key)
        if not key:
            return None
        return ImageModerationAsset.objects.filter(storage_key=key).first()

    @staticmethod
    def require_approved_public_image(storage_key: str) -> tuple[bool, str, str]:
        key = normalize_cos_key(storage_key)
        if not key:
            return True, "", ""
        asset = ImageModerationService.get_asset(key)
        if not asset:
            return False, key, "图片未建立审核记录，不能公开展示"
        if not ImageModerationService.is_moderation_enforced():
            if asset.status == ImageModerationAsset.STATUS_REJECTED:
                return False, key, "图片未审核通过，不能公开展示"
            return True, key, ""
        asset = ImageModerationService.ensure_reviewed(asset)
        if asset.status != ImageModerationAsset.STATUS_APPROVED:
            return False, key, "图片未审核通过，不能公开展示"
        return True, key, ""

    @staticmethod
    def can_manual_review(user: User | None, asset: ImageModerationAsset) -> bool:
        if not user:
            return False
        if str(getattr(user, "role", "") or "").strip().lower() in {"root", "admin"}:
            return True
        if asset.owner_type != "company" or not asset.owner_id:
            return False
        principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, asset.owner_id)
        return is_granted(principal, resource, Grant.PERM_ADMIN)

    @staticmethod
    def is_publicly_viewable(storage_key: str, mime_type: str = "") -> bool:
        key = normalize_cos_key(storage_key)
        if not key or not is_image_file(key, mime_type):
            return True
        asset = ImageModerationService.get_asset(key)
        if not asset:
            return ImageModerationService.allow_untracked()
        if not ImageModerationService.is_moderation_enforced():
            return asset.status != ImageModerationAsset.STATUS_REJECTED
        asset = ImageModerationService.ensure_reviewed(asset)
        return asset.status == ImageModerationAsset.STATUS_APPROVED

    @staticmethod
    def get_public_image_url(storage_key: str, expires_in: int = 3600) -> str | None:
        key = normalize_cos_key(storage_key)
        if not key:
            return None
        if not ImageModerationService.is_publicly_viewable(key):
            return None
        return get_public_cdn_url(key)

    @staticmethod
    def _extract_cos_detail(response: dict[str, Any]) -> dict[str, Any]:
        data = response if isinstance(response, dict) else {}
        jobs_detail = data.get("JobsDetail")
        if isinstance(jobs_detail, list) and jobs_detail:
            return jobs_detail[0] if isinstance(jobs_detail[0], dict) else {}
        if isinstance(jobs_detail, dict):
            return jobs_detail
        result = data.get("Result")
        if isinstance(result, list) and result:
            return result[0] if isinstance(result[0], dict) else {}
        if isinstance(result, dict):
            return result
        return data

    @staticmethod
    def _status_from_suggestion(suggestion: str) -> str:
        normalized = str(suggestion or "").strip().lower()
        if normalized == "pass":
            return ImageModerationAsset.STATUS_APPROVED
        if normalized == "block":
            return ImageModerationAsset.STATUS_REJECTED
        if normalized == "review":
            return ImageModerationAsset.STATUS_REVIEW_REQUIRED
        return ImageModerationAsset.STATUS_PENDING

    @staticmethod
    def _extract_score(detail: dict[str, Any]) -> float | None:
        raw = detail.get("Score") or detail.get("score")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        for key in ("PornInfo", "TerrorismInfo", "PoliticsInfo", "AdsInfo"):
            info = detail.get(key)
            if not isinstance(info, dict):
                continue
            raw_score = info.get("Score")
            try:
                return float(raw_score)
            except (TypeError, ValueError):
                continue
        return None


def moderation_payload(asset: ImageModerationAsset | None) -> dict[str, Any]:
    if not asset:
        return {"status": "untracked"}
    return asset.get_info()


class TextModerationDecision(Exception):
    def __init__(self, asset: TextModerationAsset, message: str = ""):
        self.asset = asset
        self.message = message or TextModerationService.block_message(asset)
        super().__init__(self.message)


class TextModerationService:
    PROVIDER_DISABLED = TextModerationAsset.PROVIDER_DISABLED
    PROVIDER_LOCAL = TextModerationAsset.PROVIDER_LOCAL
    PROVIDER_TENCENT = TextModerationAsset.PROVIDER_TENCENT

    @staticmethod
    def provider() -> str:
        return os.getenv("TEXT_MODERATION_PROVIDER", TextModerationService.PROVIDER_DISABLED).strip().lower()

    @staticmethod
    def fail_policy() -> str:
        policy = os.getenv("TEXT_MODERATION_FAIL_POLICY", TextModerationAsset.STATUS_REVIEW_REQUIRED).strip().lower()
        if policy == "reject":
            return TextModerationAsset.STATUS_REJECTED
        return TextModerationAsset.STATUS_REVIEW_REQUIRED

    @staticmethod
    def local_decision() -> str:
        decision = os.getenv("TEXT_MODERATION_LOCAL_DECISION", TextModerationAsset.STATUS_APPROVED).strip().lower()
        if decision not in dict(TextModerationAsset.STATUS_CHOICES):
            return TextModerationAsset.STATUS_APPROVED
        return decision

    @staticmethod
    def content_hash(text: Any) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def preview(text: Any, limit: int = 200) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        return normalized[:limit]

    @staticmethod
    def normalize_text(text: Any) -> str:
        value = str(text or "").lower()
        value = value.replace("\u3000", " ")
        return re.sub(r"\s+", "", value)

    @staticmethod
    def _event(asset: TextModerationAsset, event_type: str, *, from_status: str = "", payload: dict[str, Any] | None = None, actor_type: str = "", actor_id: str = "") -> None:
        TextModerationEvent.objects.create(
            asset=asset,
            event_type=event_type,
            from_status=from_status,
            to_status=asset.status,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
        )

    @staticmethod
    def _active_sensitive_words() -> list[dict[str, str]]:
        words = [
            {"word": item, "normalized_word": TextModerationService.normalize_text(item), "label": "env_sensitive_word", "action": "reject"}
            for item in _env_list("TEXT_MODERATION_SENSITIVE_WORDS")
        ]
        try:
            for rule in SensitiveWordRule.objects.filter(status=SensitiveWordRule.STATUS_ACTIVE):
                words.append({
                    "word": rule.word,
                    "normalized_word": rule.normalized_word or TextModerationService.normalize_text(rule.word),
                    "label": rule.label or "sensitive_word",
                    "action": rule.action or SensitiveWordRule.ACTION_REJECT,
                })
        except Exception:
            logger.exception("failed to load sensitive word rules")
        return [item for item in words if item.get("normalized_word")]

    @staticmethod
    def _match_sensitive_word(text: Any) -> dict[str, Any] | None:
        normalized = TextModerationService.normalize_text(text)
        if not normalized:
            return None
        for item in TextModerationService._active_sensitive_words():
            word = item["normalized_word"]
            if word and word in normalized:
                return item
        return None

    @staticmethod
    def check_text(
        text: Any,
        *,
        source: str,
        source_field: str = "",
        owner_type: str = "",
        owner_id: str = "",
        actor_type: str = "",
        actor_id: str = "",
        allow_blank: bool = True,
    ) -> TextModerationAsset:
        value = str(text or "")
        if not value.strip() and allow_blank:
            return TextModerationService._register_and_apply_result(
                value,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                status=TextModerationAsset.STATUS_APPROVED,
                provider=TextModerationService.PROVIDER_LOCAL,
                provider_result={"provider": "local", "decision": "blank"},
                suggestion="pass",
            )

        local_match = TextModerationService._match_sensitive_word(value)
        if local_match:
            status = (
                TextModerationAsset.STATUS_REVIEW_REQUIRED
                if local_match.get("action") == SensitiveWordRule.ACTION_REVIEW_REQUIRED
                else TextModerationAsset.STATUS_REJECTED
            )
            return TextModerationService._register_and_apply_result(
                value,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                status=status,
                provider=TextModerationService.PROVIDER_LOCAL,
                provider_result={"provider": "local", "match": TextModerationService.preview(local_match.get("word"), 80)},
                suggestion="block" if status == TextModerationAsset.STATUS_REJECTED else "review",
                label=local_match.get("label", "sensitive_word"),
                score=100,
            )

        provider = TextModerationService.provider()
        if provider == TextModerationService.PROVIDER_TENCENT:
            return TextModerationService._check_tencent(
                value,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        if provider == TextModerationService.PROVIDER_LOCAL:
            return TextModerationService._register_and_apply_result(
                value,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                status=TextModerationService.local_decision(),
                provider=provider,
                provider_result={"provider": provider, "decision": TextModerationService.local_decision()},
                suggestion=TextModerationService._suggestion_from_status(TextModerationService.local_decision()),
            )
        return TextModerationService._register_and_apply_result(
            value,
            source=source,
            source_field=source_field,
            owner_type=owner_type,
            owner_id=owner_id,
            actor_type=actor_type,
            actor_id=actor_id,
            status=TextModerationAsset.STATUS_APPROVED,
            provider=TextModerationService.PROVIDER_DISABLED,
            provider_result={"provider": TextModerationService.PROVIDER_DISABLED, "decision": "disabled_allow"},
            suggestion="pass",
        )

    @staticmethod
    def check_texts(items: list[dict[str, Any]], **context: Any) -> tuple[bool, str, TextModerationAsset | None]:
        for item in items:
            text = item.get("text")
            if text is None:
                continue
            asset = TextModerationService.check_text(
                text,
                source=item.get("source") or context.get("source") or "",
                source_field=item.get("source_field") or "",
                owner_type=item.get("owner_type") or context.get("owner_type") or "",
                owner_id=item.get("owner_id") or context.get("owner_id") or "",
                actor_type=item.get("actor_type") or context.get("actor_type") or "",
                actor_id=item.get("actor_id") or context.get("actor_id") or "",
            )
            ok, message = TextModerationService.require_approved_public_text(asset)
            if not ok:
                return False, message, asset
        return True, "", None

    @staticmethod
    def _register_and_apply_result(
        text: Any,
        *,
        source: str,
        source_field: str = "",
        owner_type: str = "",
        owner_id: str = "",
        actor_type: str = "",
        actor_id: str = "",
        status: str,
        provider: str,
        provider_result: dict[str, Any] | None = None,
        suggestion: str = "",
        label: str = "",
        score: float | None = None,
        failure_reason: str = "",
    ) -> TextModerationAsset:
        content_hash = TextModerationService.content_hash(text)
        now = _now_ts()
        asset, created = TextModerationAsset.objects.update_or_create(
            content_hash=content_hash,
            source=source,
            source_field=source_field or "",
            owner_type=owner_type or "",
            owner_id=owner_id or "",
            defaults={
                "content_preview": TextModerationService.preview(text),
                "actor_type": actor_type or "",
                "actor_id": actor_id or "",
                "provider": provider or TextModerationService.provider(),
                "updated_at": now,
            },
        )
        previous_status = asset.status
        asset.status = status if status in dict(TextModerationAsset.STATUS_CHOICES) else TextModerationAsset.STATUS_PENDING
        asset.provider = provider or asset.provider
        asset.provider_result = provider_result or {}
        asset.suggestion = suggestion or TextModerationService._suggestion_from_status(asset.status)
        asset.label = label or asset.label
        asset.score = score if score is not None else asset.score
        asset.failure_reason = failure_reason or ""
        asset.updated_at = now
        asset.save()
        TextModerationService._event(
            asset,
            "created" if created else "checked",
            from_status=previous_status,
            payload={"provider": asset.provider, "suggestion": asset.suggestion, "label": asset.label},
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return asset

    @staticmethod
    def _check_tencent(
        text: str,
        *,
        source: str,
        source_field: str = "",
        owner_type: str = "",
        owner_id: str = "",
        actor_type: str = "",
        actor_id: str = "",
    ) -> TextModerationAsset:
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.tms.v20201229 import models, tms_client

            secret_id = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
            secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()
            if not secret_id or not secret_key:
                raise RuntimeError("Tencent Cloud text moderation credentials are not configured")

            region = os.getenv("TEXT_MODERATION_TENCENT_REGION", "ap-guangzhou").strip()
            biz_type = os.getenv("TEXT_MODERATION_TENCENT_BIZ_TYPE", "").strip()
            cred = credential.Credential(secret_id, secret_key)
            http_profile = HttpProfile()
            http_profile.endpoint = "tms.tencentcloudapi.com"
            client_profile = ClientProfile()
            client_profile.httpProfile = http_profile
            client = tms_client.TmsClient(cred, region, client_profile)
            req = models.TextModerationRequest()
            params = {
                "Content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            }
            if biz_type:
                params["BizType"] = biz_type
            req.from_json_string(json.dumps(params))
            response = json.loads(client.TextModeration(req).to_json_string())
            suggestion = str(response.get("Suggestion") or "").strip().lower()
            label = str(response.get("Label") or "").strip()
            score = TextModerationService._extract_tencent_score(response)
            status = TextModerationService._status_from_suggestion(suggestion)
            return TextModerationService._register_and_apply_result(
                text,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                status=status,
                provider=TextModerationService.PROVIDER_TENCENT,
                provider_result=response,
                suggestion=suggestion,
                label=label,
                score=score,
            )
        except Exception as exc:
            logger.exception("Tencent text moderation failed source=%s field=%s", source, source_field)
            return TextModerationService._register_and_apply_result(
                text,
                source=source,
                source_field=source_field,
                owner_type=owner_type,
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                status=TextModerationService.fail_policy(),
                provider=TextModerationService.PROVIDER_TENCENT,
                provider_result={"error": str(exc)},
                suggestion=TextModerationService._suggestion_from_status(TextModerationService.fail_policy()),
                failure_reason=str(exc),
            )

    @staticmethod
    def apply_result(
        asset: TextModerationAsset,
        *,
        status: str,
        reviewer: str = "",
        reason: str = "",
    ) -> TextModerationAsset:
        previous_status = asset.status
        asset.status = status if status in dict(TextModerationAsset.STATUS_CHOICES) else TextModerationAsset.STATUS_PENDING
        asset.failure_reason = reason or ""
        if reviewer:
            asset.reviewed_by = reviewer
            asset.reviewed_at = _now_ts()
        asset.updated_at = _now_ts()
        asset.save()
        TextModerationService._event(
            asset,
            "manual_review",
            from_status=previous_status,
            payload={"reason": reason},
            actor_type="user",
            actor_id=reviewer,
        )
        return asset

    @staticmethod
    def approve(asset_id: int, reviewer: str = "") -> TextModerationAsset | None:
        asset = TextModerationAsset.objects.filter(id=asset_id).first()
        if not asset:
            return None
        return TextModerationService.apply_result(asset, status=TextModerationAsset.STATUS_APPROVED, reviewer=reviewer)

    @staticmethod
    def reject(asset_id: int, reviewer: str = "", reason: str = "") -> TextModerationAsset | None:
        asset = TextModerationAsset.objects.filter(id=asset_id).first()
        if not asset:
            return None
        return TextModerationService.apply_result(asset, status=TextModerationAsset.STATUS_REJECTED, reviewer=reviewer, reason=reason)

    @staticmethod
    def require_approved_public_text(asset: TextModerationAsset | None) -> tuple[bool, str]:
        if asset is None:
            return False, "TEXT_MODERATION_UNTRACKED: 文本未建立审核记录，不能公开展示或写入"
        if asset.status == TextModerationAsset.STATUS_APPROVED:
            return True, ""
        return False, TextModerationService.block_message(asset)

    @staticmethod
    def block_message(asset: TextModerationAsset) -> str:
        code = "TEXT_MODERATION_BLOCKED"
        if asset.status == TextModerationAsset.STATUS_REVIEW_REQUIRED:
            code = "TEXT_MODERATION_REVIEW_REQUIRED"
        elif asset.status == TextModerationAsset.STATUS_FAILED:
            code = "TEXT_MODERATION_FAILED"
        elif asset.status == TextModerationAsset.STATUS_PENDING:
            code = "TEXT_MODERATION_PENDING"
        return f"{code}: 文本内容未通过安全审核"

    @staticmethod
    def can_manual_review(user: User | None, asset: TextModerationAsset) -> bool:
        if not user:
            return False
        if str(getattr(user, "role", "") or "").strip().lower() in {"root", "admin"}:
            return True
        if asset.owner_type != "company" or not asset.owner_id:
            return False
        principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, asset.owner_id)
        return is_granted(principal, resource, Grant.PERM_ADMIN)

    @staticmethod
    def can_view_status(user: User | None, asset: TextModerationAsset) -> bool:
        if not user:
            return False
        if str(getattr(user, "role", "") or "").strip().lower() in {"root", "admin"}:
            return True
        user_uid = str(getattr(user, "uid", "") or "")
        user_id = str(getattr(user, "id", "") or "")
        if asset.actor_type == "user" and str(asset.actor_id or "") in {user_uid, user_id}:
            return True
        if asset.owner_type == "company" and asset.owner_id:
            principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
            resource = get_or_create_resource(Resource.TYPE_COMPANY, asset.owner_id)
            return is_granted(principal, resource, Grant.PERM_ADMIN)
        return False

    @staticmethod
    def _status_from_suggestion(suggestion: str) -> str:
        normalized = str(suggestion or "").strip().lower()
        if normalized == "pass":
            return TextModerationAsset.STATUS_APPROVED
        if normalized == "block":
            return TextModerationAsset.STATUS_REJECTED
        if normalized == "review":
            return TextModerationAsset.STATUS_REVIEW_REQUIRED
        return TextModerationAsset.STATUS_PENDING

    @staticmethod
    def _suggestion_from_status(status: str) -> str:
        if status == TextModerationAsset.STATUS_APPROVED:
            return "pass"
        if status == TextModerationAsset.STATUS_REJECTED:
            return "block"
        if status == TextModerationAsset.STATUS_REVIEW_REQUIRED:
            return "review"
        return ""

    @staticmethod
    def _extract_tencent_score(response: dict[str, Any]) -> float | None:
        for key in ("Score", "RiskDetails"):
            value = response.get(key)
            if isinstance(value, (int, float, str)):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    score = item.get("Score")
                    try:
                        return float(score)
                    except (TypeError, ValueError):
                        continue
        return None


def text_moderation_payload(asset: TextModerationAsset | None) -> dict[str, Any]:
    if not asset:
        return {"status": "untracked"}
    return asset.get_info()
