import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError

from apps.risk_control.models import (
    RiskAllowListEntry,
    RiskBlockListEntry,
    RiskDecisionLog,
    RiskManualAction,
    RiskRule,
)

logger = logging.getLogger("touming")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"
    status_code: int = 200
    retry_after: int = 0
    matched_rule: str = ""
    decision: str = RiskDecisionLog.DECISION_ALLOW

    def to_response_payload(self) -> dict[str, Any]:
        return {
            "result": 0,
            "message": self.reason,
            "code": self.status_code,
            "retry_after": self.retry_after,
            "risk_rule": self.matched_rule,
        }


def _now_ts() -> str:
    return str(time.time())[:20]


def _is_enabled() -> bool:
    return bool(getattr(settings, "RISK_CONTROL_ENABLED", True))


def _fail_policy() -> str:
    return str(getattr(settings, "RISK_CONTROL_FAIL_POLICY", "deny") or "deny").lower()


def _prefix() -> str:
    return str(getattr(settings, "RISK_CONTROL_REDIS_PREFIX", "touming:risk") or "touming:risk")


def _hash_value(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    salt = str(getattr(settings, "RISK_CONTROL_HASH_SALT", getattr(settings, "SECRET_KEY", "")) or "")
    return hashlib.sha256(f"{salt}:{raw_value}".encode("utf-8")).hexdigest()


def _mask_phone(phone: Any) -> str:
    value = str(phone or "")
    if len(value) <= 7:
        return f"{value[:2]}***{value[-2:]}" if value else ""
    return f"{value[:3]}****{value[-4:]}"


def _mask_ip(client_ip: Any) -> str:
    value = str(client_ip or "")
    if not value:
        return ""
    parts = value.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2] + ["*", "*"])
    if ":" in value:
        chunks = value.split(":")
        return ":".join(chunks[:2] + ["*"])
    return "***"


def _mask_dimension(dimension: str, value: Any) -> str:
    if dimension == RiskRule.DIMENSION_PHONE:
        return _mask_phone(value)
    if dimension == RiskRule.DIMENSION_IP:
        return _mask_ip(value)
    if dimension == RiskRule.DIMENSION_API_KEY:
        raw = str(value or "")
        return f"{raw[:8]}..." if raw else ""
    return str(value or "")[:128]


def _rate_limit_message(seconds: int) -> str:
    seconds = max(int(seconds or 0), 1)
    if seconds >= 60:
        minutes = seconds // 60
        remain_seconds = seconds % 60
        if remain_seconds:
            return f"请求过于频繁，请等待 {minutes} 分 {remain_seconds} 秒后重试"
        return f"请求过于频繁，请等待 {minutes} 分钟后重试"
    return f"请求过于频繁，请等待 {seconds} 秒后重试"


def _window_key(scope: str, action: str, dimension: str, value_hash: str, window_seconds: int) -> str:
    bucket = int(time.time() // max(int(window_seconds or 1), 1))
    return f"{_prefix()}:count:{scope}:{action}:{dimension}:{value_hash}:{window_seconds}:{bucket}"


def _block_key(scope: str, dimension: str, value_hash: str) -> str:
    return f"{_prefix()}:block:{scope}:{dimension}:{value_hash}"


class RiskControlService:
    DEFAULT_RULES: dict[str, list[dict[str, Any]]] = {
        "sms.send": [
            {"dimension": "phone", "limit": 1, "window_seconds": 60},
            {"dimension": "phone", "limit": 5, "window_seconds": 600, "block_seconds": 1800},
            {"dimension": "phone", "limit": 10, "window_seconds": 86400},
            {"dimension": "ip", "limit": 5, "window_seconds": 60, "block_seconds": 3600},
            {"dimension": "ip", "limit": 30, "window_seconds": 3600, "block_seconds": 3600},
            {"dimension": "ip_phone", "limit": 3, "window_seconds": 600},
        ],
        "sms.verify_failure": [
            {"dimension": "phone", "limit": 5, "window_seconds": 600, "block_seconds": 1800, "action": "failure_limit"},
        ],
        "auth.password_login": [
            {"dimension": "ip", "limit": 30, "window_seconds": 60},
            {"dimension": "phone", "limit": 10, "window_seconds": 900, "action": "failure_limit", "block_seconds": 900},
        ],
        "auth.register": [
            {"dimension": "ip", "limit": 20, "window_seconds": 3600},
            {"dimension": "phone", "limit": 10, "window_seconds": 86400},
        ],
        "api_key.call": [
            {"dimension": "api_key", "limit": 120, "window_seconds": 60},
            {"dimension": "api_key_ip", "limit": 120, "window_seconds": 60},
        ],
        "api_key.verify": [
            {"dimension": "ip", "limit": 30, "window_seconds": 60, "block_seconds": 300},
            {
                "dimension": "api_key",
                "limit": 10,
                "window_seconds": 300,
                "block_seconds": 900,
                "action": "failure_limit",
            },
            {
                "dimension": "api_key_ip",
                "limit": 10,
                "window_seconds": 300,
                "block_seconds": 900,
                "action": "failure_limit",
            },
        ],
        "api_key.create": [
            {"dimension": "user", "limit": 20, "window_seconds": 3600},
            {"dimension": "ip", "limit": 60, "window_seconds": 3600},
        ],
        "external_api.upsert": [
            {"dimension": "api_key", "limit": 60, "window_seconds": 60},
            {"dimension": "resource", "limit": 120, "window_seconds": 60},
        ],
        "runtime.openclaw.call": [
            {"dimension": "user", "limit": 30, "window_seconds": 60},
            {"dimension": "company", "limit": 120, "window_seconds": 60},
            {"dimension": "agent", "limit": 60, "window_seconds": 60},
        ],
    }

    ENV_OVERRIDES = {
        "sms.send": {
            ("ip", 3600): "RISK_CONTROL_SMS_IP_LIMIT_PER_HOUR",
            ("phone", 86400): "RISK_CONTROL_SMS_PHONE_LIMIT_PER_DAY",
        },
        "auth.password_login": {
            ("phone", 900): "RISK_CONTROL_LOGIN_FAILURE_LIMIT_PER_15M",
        },
        "api_key.call": {
            ("api_key", 60): "RISK_CONTROL_API_KEY_LIMIT_PER_MINUTE",
            ("api_key_ip", 60): "RISK_CONTROL_API_KEY_LIMIT_PER_MINUTE",
        },
        "runtime.openclaw.call": {
            ("user", 60): "RISK_CONTROL_RUNTIME_CALL_LIMIT_PER_MINUTE",
        },
    }

    @staticmethod
    def hash_value(value: Any) -> str:
        return _hash_value(value)

    @staticmethod
    def build_context(request=None, **dimensions) -> dict[str, Any]:
        context = dict(dimensions)
        if request is not None:
            try:
                from common.utils.Login_utils import get_client_ip

                context.setdefault("ip", get_client_ip(request))
            except Exception:
                context.setdefault("ip", getattr(request, "META", {}).get("REMOTE_ADDR", ""))
            context.setdefault("request_path", getattr(request, "path", "")[:255])
            context.setdefault("request_method", getattr(request, "method", ""))
            context.setdefault("user_agent", getattr(request, "META", {}).get("HTTP_USER_AGENT", ""))
            context.setdefault("actor_user_id", getattr(request, "user_id", "") or "")
            api_key = getattr(request, "api_key", None)
            if api_key is not None:
                context.setdefault("api_key_id", getattr(api_key, "secret_id", "") or "")
        return context

    @staticmethod
    def _dimension_value(dimension: str, context: dict[str, Any]) -> str:
        if dimension == "api_key":
            return str(context.get("api_key_id") or context.get("api_key") or "")
        if dimension == "user":
            return str(context.get("actor_user_id") or context.get("user_id") or "")
        if dimension == "phone":
            return str(context.get("phone") or "")
        if dimension == "company":
            return str(context.get("company_id") or context.get("company") or "")
        if dimension == "agent":
            return str(context.get("agent_id") or context.get("agent") or "")
        if dimension == "resource":
            return str(context.get("resource_id") or context.get("tid") or context.get("gid") or "")
        if dimension == "user_agent":
            return str(context.get("user_agent") or "")
        if dimension == "api_key_ip":
            return f"{context.get('api_key_id') or context.get('api_key') or ''}:{context.get('ip') or ''}"
        if dimension == "ip_phone":
            return f"{context.get('ip') or ''}:{context.get('phone') or ''}"
        return str(context.get(dimension) or "")

    @staticmethod
    def _rule_limit(scope: str, rule: dict[str, Any]) -> int:
        dimension = str(rule.get("dimension") or "")
        window = int(rule.get("window_seconds") or 60)
        env_name = RiskControlService.ENV_OVERRIDES.get(scope, {}).get((dimension, window))
        if env_name:
            return int(getattr(settings, env_name, rule.get("limit") or 1) or rule.get("limit") or 1)
        return int(rule.get("limit") or 1)

    @staticmethod
    def _default_rules(scope: str) -> list[dict[str, Any]]:
        rules = [dict(item) for item in RiskControlService.DEFAULT_RULES.get(scope, [])]
        if not rules:
            rules = [
                {
                    "dimension": "ip",
                    "limit": int(getattr(settings, "RISK_CONTROL_DEFAULT_IP_LIMIT_PER_MINUTE", 60) or 60),
                    "window_seconds": 60,
                },
                {
                    "dimension": "user",
                    "limit": int(getattr(settings, "RISK_CONTROL_DEFAULT_USER_LIMIT_PER_MINUTE", 120) or 120),
                    "window_seconds": 60,
                },
            ]
        return rules

    @staticmethod
    def _rules_for_scope(scope: str, action: str = RiskRule.ACTION_RATE_LIMIT) -> list[dict[str, Any]]:
        try:
            db_rules = list(
                RiskRule.objects.filter(scope=scope, enabled=True)
                .filter(action__in=[action, RiskRule.ACTION_RATE_LIMIT])
                .order_by("id")
            )
        except Exception:
            db_rules = []
        if db_rules:
            return [
                {
                    "dimension": rule.dimension,
                    "limit": rule.limit,
                    "window_seconds": rule.window_seconds,
                    "block_seconds": rule.block_seconds,
                    "action": rule.action,
                    "source": f"db:{rule.id}",
                }
                for rule in db_rules
            ]
        return RiskControlService._default_rules(scope)

    @staticmethod
    def _active_list_entry(model, scope: str, dimension: str, value_hash: str):
        if not value_hash:
            return None
        now = time.time()
        try:
            candidates = model.objects.filter(
                dimension=dimension,
                value_hash=value_hash,
                enabled=True,
            ).filter(scope__in=["", scope]).order_by("-id")
            for entry in candidates:
                if entry.expires_at and float(entry.expires_at) <= now:
                    continue
                return entry
        except Exception:
            return None
        return None

    @staticmethod
    def is_allowed(scope: str, context: dict[str, Any]) -> RiskDecision | None:
        for dimension in ["ip", "phone", "user", "api_key", "company", "agent", "resource", "user_agent"]:
            value_hash = _hash_value(RiskControlService._dimension_value(dimension, context))
            entry = RiskControlService._active_list_entry(RiskAllowListEntry, scope, dimension, value_hash)
            if entry:
                return RiskDecision(
                    allowed=True,
                    reason=entry.reason or "allow list",
                    matched_rule=f"allow_list:{dimension}",
                    decision=RiskDecisionLog.DECISION_ALLOW,
                )
        return None

    @staticmethod
    def is_blocked(scope: str, context: dict[str, Any]) -> RiskDecision | None:
        for dimension in [
            "ip",
            "phone",
            "user",
            "api_key",
            "company",
            "agent",
            "resource",
            "user_agent",
        ]:
            value = RiskControlService._dimension_value(dimension, context)
            value_hash = _hash_value(value)
            entry = RiskControlService._active_list_entry(RiskBlockListEntry, scope, dimension, value_hash)
            if entry:
                return RiskDecision(
                    allowed=False,
                    reason=entry.reason or "请求已被风控拦截",
                    status_code=403,
                    matched_rule=f"block_list:{dimension}",
                    decision=RiskDecisionLog.DECISION_DENY,
                )
            cache_until = cache.get(_block_key(scope, dimension, value_hash)) if value_hash else None
            if cache_until:
                retry_after = max(int(float(cache_until) - time.time()), 1)
                return RiskDecision(
                    allowed=False,
                    reason=_rate_limit_message(retry_after),
                    status_code=429,
                    retry_after=retry_after,
                    matched_rule=f"temporary_block:{dimension}",
                    decision=RiskDecisionLog.DECISION_DENY,
                )
        return None

    @staticmethod
    def allow_multiplier(scope: str, context: dict[str, Any]) -> int:
        multiplier = 1
        for dimension in ["ip", "phone", "user", "api_key", "company", "agent", "resource", "user_agent"]:
            value_hash = _hash_value(RiskControlService._dimension_value(dimension, context))
            entry = RiskControlService._active_list_entry(RiskAllowListEntry, scope, dimension, value_hash)
            if entry:
                multiplier = max(multiplier, int(getattr(entry, "multiplier", 1) or 1))
        return multiplier

    @staticmethod
    def check_rate_limit(scope: str, context: dict[str, Any], *, action: str = RiskRule.ACTION_RATE_LIMIT) -> RiskDecision:
        multiplier = RiskControlService.allow_multiplier(scope, context)
        for rule in RiskControlService._rules_for_scope(scope, action=action):
            rule_action = str(rule.get("action") or RiskRule.ACTION_RATE_LIMIT)
            if rule_action != action:
                continue
            dimension = str(rule.get("dimension") or "")
            raw_value = RiskControlService._dimension_value(dimension, context)
            value_hash = _hash_value(raw_value)
            if not raw_value or not value_hash:
                continue
            window = int(rule.get("window_seconds") or 60)
            limit = RiskControlService._rule_limit(scope, rule) * multiplier
            key = _window_key(scope, action, dimension, value_hash, window)
            try:
                try:
                    count = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, timeout=window + 5)
                    count = 1
                if count <= limit:
                    continue

                retry_after = window - (int(time.time()) % window)
                block_seconds = int(rule.get("block_seconds") or 0)
                if block_seconds > 0:
                    cache.set(
                        _block_key(scope, dimension, value_hash),
                        time.time() + block_seconds,
                        timeout=block_seconds,
                    )
                    retry_after = block_seconds
                return RiskDecision(
                    allowed=False,
                    reason=_rate_limit_message(retry_after),
                    status_code=429,
                    retry_after=retry_after,
                    matched_rule=f"{scope}:{action}:{dimension}:{limit}/{window}",
                    decision=RiskDecisionLog.DECISION_DENY,
                )
            except Exception as exc:
                logger.exception("[risk-control] cache check failed scope=%s dimension=%s", scope, dimension)
                if _fail_policy() == "deny":
                    return RiskDecision(
                        allowed=False,
                        reason="风控服务暂不可用",
                        status_code=503,
                        matched_rule="cache_unavailable",
                        decision=RiskDecisionLog.DECISION_DENY,
                    )
                logger.warning("[risk-control] fail-open scope=%s err=%s", scope, exc)
        return RiskDecision(allowed=True)

    @staticmethod
    def evaluate(scope: str, request=None, **dimensions) -> RiskDecision:
        if not _is_enabled():
            return RiskDecision(allowed=True, reason="risk control disabled")
        context = RiskControlService.build_context(request, **dimensions)
        allowed = RiskControlService.is_allowed(scope, context)
        if allowed:
            if bool(getattr(settings, "RISK_CONTROL_LOG_ALLOW_DECISIONS", False)):
                RiskControlService.record_decision(scope, allowed, context)
            return allowed
        blocked = RiskControlService.is_blocked(scope, context)
        if blocked:
            RiskControlService.record_decision(scope, blocked, context)
            return blocked
        decision = RiskControlService.check_rate_limit(scope, context)
        if not decision.allowed or bool(getattr(settings, "RISK_CONTROL_LOG_ALLOW_DECISIONS", False)):
            RiskControlService.record_decision(scope, decision, context)
        return decision

    @staticmethod
    def record_failure(scope: str, request=None, **dimensions) -> RiskDecision:
        context = RiskControlService.build_context(request, **dimensions)
        decision = RiskControlService.check_rate_limit(scope, context, action=RiskRule.ACTION_FAILURE_LIMIT)
        if not decision.allowed or bool(getattr(settings, "RISK_CONTROL_LOG_ALLOW_DECISIONS", False)):
            RiskControlService.record_decision(scope, decision, context)
        return decision

    @staticmethod
    def reset_failure(scope: str, **dimensions) -> None:
        # Fixed-window counters are intentionally allowed to expire naturally.
        return None

    @staticmethod
    def record_decision(scope: str, decision: RiskDecision, context: dict[str, Any]) -> None:
        try:
            RiskDecisionLog.objects.create(
                scope=scope,
                decision=decision.decision,
                reason=decision.reason[:255],
                matched_rule=decision.matched_rule[:255],
                retry_after=decision.retry_after,
                actor_user_id=str(context.get("actor_user_id") or context.get("user_id") or "")[:128],
                company_id=str(context.get("company_id") or "")[:128],
                api_key_id=str(context.get("api_key_id") or context.get("api_key") or "")[:128],
                resource_id=str(context.get("resource_id") or context.get("tid") or context.get("gid") or "")[:128],
                ip_hash=_hash_value(context.get("ip")),
                ip_masked=_mask_ip(context.get("ip")),
                phone_hash=_hash_value(context.get("phone")),
                user_agent_hash=_hash_value(context.get("user_agent")),
                request_path=str(context.get("request_path") or "")[:255],
                request_method=str(context.get("request_method") or "")[:16],
                dimensions=RiskControlService.safe_dimensions(context),
                created_at=_now_ts(),
            )
        except (DatabaseError, AssertionError):
            logger.warning("[risk-control] decision log skipped scope=%s", scope)
        except Exception:
            logger.exception("[risk-control] decision log failed scope=%s", scope)

    @staticmethod
    def safe_dimensions(context: dict[str, Any]) -> dict[str, Any]:
        safe = {}
        for key, value in context.items():
            if key in {"phone", "ip", "user_agent"}:
                safe[f"{key}_hash"] = _hash_value(value)
                if key == "phone":
                    safe["phone_masked"] = _mask_phone(value)
                elif key == "ip":
                    safe["ip_masked"] = _mask_ip(value)
                continue
            if key in {"secret_key", "token", "captcha_token", "code", "password"}:
                continue
            safe[key] = str(value)[:255] if value is not None else ""
        return safe

    @staticmethod
    def apply_manual_action(
        *,
        action: str,
        dimension: str,
        value: Any,
        scope: str = "",
        reason: str = "",
        operator_id: str = "",
        expires_in: int = 0,
        multiplier: int = 1,
    ):
        value_hash = _hash_value(value)
        value_masked = _mask_dimension(dimension, value)
        now = _now_ts()
        expires_at = str(time.time() + int(expires_in))[:20] if expires_in else ""
        if action == RiskManualAction.ACTION_BLOCK:
            entry, _ = RiskBlockListEntry.objects.update_or_create(
                dimension=dimension,
                value_hash=value_hash,
                scope=scope or "",
                defaults={
                    "value_masked": value_masked,
                    "reason": reason,
                    "expires_at": expires_at,
                    "enabled": True,
                    "created_by": operator_id,
                    "updated_at": now,
                    "created_at": now,
                },
            )
        elif action == RiskManualAction.ACTION_UNBLOCK:
            RiskBlockListEntry.objects.filter(dimension=dimension, value_hash=value_hash, scope=scope or "").update(
                enabled=False,
                updated_at=now,
            )
            entry = None
        elif action == RiskManualAction.ACTION_ALLOW:
            entry, _ = RiskAllowListEntry.objects.update_or_create(
                dimension=dimension,
                value_hash=value_hash,
                scope=scope or "",
                defaults={
                    "value_masked": value_masked,
                    "reason": reason,
                    "expires_at": expires_at,
                    "enabled": True,
                    "created_by": operator_id,
                    "updated_at": now,
                    "created_at": now,
                    "multiplier": max(int(multiplier or 1), 1),
                },
            )
        else:
            RiskAllowListEntry.objects.filter(dimension=dimension, value_hash=value_hash, scope=scope or "").update(
                enabled=False,
                updated_at=now,
            )
            entry = None

        RiskManualAction.objects.create(
            action=action,
            dimension=dimension,
            value_hash=value_hash,
            value_masked=value_masked,
            scope=scope or "",
            reason=reason,
            operator_id=operator_id,
            created_at=now,
        )
        return entry
