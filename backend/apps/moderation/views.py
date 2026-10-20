import json
import logging
import os

from django.http import JsonResponse
from rest_framework.decorators import api_view

from apps.moderation.models import ImageModerationAsset, TextModerationAsset
from apps.moderation.serializers import (
    ImageModerationManualReviewSerializer,
    ImageModerationStatusSerializer,
    TextModerationCheckSerializer,
    TextModerationManualReviewSerializer,
    TextModerationStatusSerializer,
)
from apps.moderation.services import (
    ImageModerationService,
    TextModerationService,
    moderation_payload,
    text_moderation_payload,
)
from common.utils.Login_utils import get_client_ip, login_checker
from common.utils.common_utils import get_user_from_request


logger = logging.getLogger("touming")


@api_view(["GET", "POST"])
@login_checker
def image_moderation_status(request):
    data = request.GET if request.method == "GET" else request.data
    serializer = ImageModerationStatusSerializer(data=data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    asset = ImageModerationService.get_asset(serializer.validated_data["storage_key"])
    if not asset:
        return JsonResponse({"result": 0, "message": "审核记录不存在"}, status=404)
    if asset.status == ImageModerationAsset.STATUS_PENDING and asset.provider_job_id:
        asset = ImageModerationService.refresh_cos_result(asset)
    return JsonResponse({"result": 1, "message": "success", "data": moderation_payload(asset)})


@api_view(["POST"])
@login_checker
def image_moderation_manual_review(request):
    serializer = ImageModerationManualReviewSerializer(data=request.data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    user_id = str(getattr(request, "user_id", "") or "")
    storage_key = serializer.validated_data["storage_key"]
    asset = ImageModerationService.get_asset(storage_key)
    if not asset:
        return JsonResponse({"result": 0, "message": "审核记录不存在"}, status=404)

    user = get_user_from_request(request)
    if not ImageModerationService.can_manual_review(user, asset):
        return JsonResponse({"result": 0, "message": "无审核权限"}, status=403)

    action = serializer.validated_data["action"]
    reason = serializer.validated_data.get("reason", "")
    if action == "approve":
        asset = ImageModerationService.approve(storage_key, reviewer=user_id)
    else:
        asset = ImageModerationService.reject(storage_key, reviewer=user_id, reason=reason)
    logger.info(
        "[image-moderation-manual-review] action=%s storage_key=%s reviewer=%s ip=%s",
        action,
        storage_key,
        user_id,
        get_client_ip(request),
    )
    return JsonResponse({"result": 1, "message": "success", "data": moderation_payload(asset)})


@api_view(["POST"])
def image_moderation_cos_callback(request):
    expected_secret = os.getenv("IMAGE_MODERATION_CALLBACK_SECRET", "").strip()
    if not expected_secret:
        return JsonResponse({"result": 0, "message": "callback secret is not configured"}, status=403)
    actual_secret = request.META.get("HTTP_X_IMAGE_MODERATION_SECRET", "").strip()
    if actual_secret != expected_secret:
        return JsonResponse({"result": 0, "message": "forbidden"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        payload = {}
    detail = ImageModerationService._extract_cos_detail(payload)
    storage_key = (
        detail.get("Object")
        or detail.get("Key")
        or detail.get("Name")
        or detail.get("Url")
        or request.GET.get("storage_key")
    )
    if not storage_key:
        return JsonResponse({"result": 0, "message": "storage_key missing"}, status=400)
    asset = ImageModerationService.get_asset(str(storage_key))
    if not asset:
        return JsonResponse({"result": 0, "message": "审核记录不存在"}, status=404)
    asset = ImageModerationService.apply_cos_response(asset, payload)
    return JsonResponse({"result": 1, "message": "success", "data": moderation_payload(asset)})


@api_view(["POST"])
@login_checker
def text_moderation_check(request):
    serializer = TextModerationCheckSerializer(data=request.data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    user = get_user_from_request(request)
    asset = TextModerationService.check_text(
        serializer.validated_data["text"],
        source=serializer.validated_data.get("source") or "manual.check",
        source_field=serializer.validated_data.get("source_field") or "text",
        owner_type=serializer.validated_data.get("owner_type") or "",
        owner_id=serializer.validated_data.get("owner_id") or "",
        actor_type="user",
        actor_id=getattr(user, "uid", "") if user else "",
    )
    ok, message = TextModerationService.require_approved_public_text(asset)
    return JsonResponse({
        "result": 1 if ok else 0,
        "message": "success" if ok else message,
        "data": text_moderation_payload(asset),
    }, status=200 if ok else 403)


@api_view(["GET", "POST"])
@login_checker
def text_moderation_status(request):
    data = request.GET if request.method == "GET" else request.data
    serializer = TextModerationStatusSerializer(data=data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    qs = TextModerationAsset.objects.all()
    if serializer.validated_data.get("asset_id"):
        qs = qs.filter(id=serializer.validated_data["asset_id"])
    else:
        qs = qs.filter(content_hash=serializer.validated_data["content_hash"])
    asset = qs.order_by("-id").first()
    if not asset:
        return JsonResponse({"result": 0, "message": "审核记录不存在"}, status=404)
    user = get_user_from_request(request)
    if not TextModerationService.can_view_status(user, asset):
        return JsonResponse({"result": 0, "message": "无权限查看审核记录"}, status=403)
    return JsonResponse({"result": 1, "message": "success", "data": text_moderation_payload(asset)})


@api_view(["POST"])
@login_checker
def text_moderation_manual_review(request):
    serializer = TextModerationManualReviewSerializer(data=request.data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    asset = TextModerationAsset.objects.filter(id=serializer.validated_data["asset_id"]).first()
    if not asset:
        return JsonResponse({"result": 0, "message": "审核记录不存在"}, status=404)
    user = get_user_from_request(request)
    if not TextModerationService.can_manual_review(user, asset):
        return JsonResponse({"result": 0, "message": "无审核权限"}, status=403)

    user_id = str(getattr(request, "user_id", "") or getattr(user, "uid", "") or "")
    action = serializer.validated_data["action"]
    reason = serializer.validated_data.get("reason", "")
    if action == "approve":
        asset = TextModerationService.approve(asset.id, reviewer=user_id)
    else:
        asset = TextModerationService.reject(asset.id, reviewer=user_id, reason=reason)
    logger.info(
        "[text-moderation-manual-review] action=%s asset_id=%s reviewer=%s ip=%s",
        action,
        serializer.validated_data["asset_id"],
        user_id,
        get_client_ip(request),
    )
    return JsonResponse({"result": 1, "message": "success", "data": text_moderation_payload(asset)})
