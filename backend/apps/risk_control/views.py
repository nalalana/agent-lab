from django.http import JsonResponse

from apps.risk_control.models import RiskAllowListEntry, RiskBlockListEntry, RiskDecisionLog
from apps.risk_control.serializers import RiskDecisionLogListSerializer, RiskManualActionSerializer
from apps.risk_control.services import RiskControlService
from apps.user.models import User
from apps.user.permissions import is_platform_administrator
from common.utils.Login_utils import login_checker


def _require_ops_user(request):
    try:
        user = User.objects.get(id=request.user_id)
    except Exception:
        return None, JsonResponse({"result": 0, "message": "user not found"}, status=404)
    if not is_platform_administrator(user):
        return None, JsonResponse({"result": 0, "message": "user is not admin"}, status=403)
    return user, None


def _require_admin_user(request):
    user, error = _require_ops_user(request)
    if error:
        return None, error
    return user, None


@login_checker
def decision_logs(request):
    user, error = _require_ops_user(request)
    if error:
        return error
    serializer = RiskDecisionLogListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    data = serializer.validated_data
    queryset = RiskDecisionLog.objects.all().order_by("-id")
    for field in ["scope", "decision", "actor_user_id", "company_id", "api_key_id"]:
        value = data.get(field)
        if value:
            queryset = queryset.filter(**{field: value})
    total = queryset.count()
    rows = list(queryset[data["offset"] : data["offset"] + data["limit"]])
    return JsonResponse(
        {
            "result": 1,
            "message": "success",
            "data": {
                "total": total,
                "items": [
                    {
                        "id": item.id,
                        "scope": item.scope,
                        "decision": item.decision,
                        "reason": item.reason,
                        "matched_rule": item.matched_rule,
                        "retry_after": item.retry_after,
                        "actor_user_id": item.actor_user_id,
                        "company_id": item.company_id,
                        "api_key_id": item.api_key_id,
                        "resource_id": item.resource_id,
                        "ip_masked": item.ip_masked,
                        "request_path": item.request_path,
                        "request_method": item.request_method,
                        "created_at": item.created_at,
                    }
                    for item in rows
                ],
            },
        }
    )


@login_checker
def list_entries(request):
    user, error = _require_ops_user(request)
    if error:
        return error
    entry_type = request.GET.get("type", "block")
    model = RiskAllowListEntry if entry_type == "allow" else RiskBlockListEntry
    rows = list(model.objects.filter(enabled=True).order_by("-id")[:200])
    return JsonResponse(
        {
            "result": 1,
            "message": "success",
            "data": [
                {
                    "id": item.id,
                    "dimension": item.dimension,
                    "value_masked": item.value_masked,
                    "scope": item.scope,
                    "reason": item.reason,
                    "expires_at": item.expires_at,
                    "created_by": item.created_by,
                    "created_at": item.created_at,
                }
                for item in rows
            ],
        }
    )


@login_checker
def manual_action(request):
    user, error = _require_admin_user(request)
    if error:
        return error
    serializer = RiskManualActionSerializer(data=request.POST or getattr(request, "data", None))
    if not serializer.is_valid():
        import json

        try:
            serializer = RiskManualActionSerializer(data=json.loads(request.body.decode() or "{}"))
        except Exception:
            pass
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    data = serializer.validated_data
    entry = RiskControlService.apply_manual_action(
        action=data["action"],
        dimension=data["dimension"],
        value=data["value"],
        scope=data.get("scope", ""),
        reason=data.get("reason", ""),
        operator_id=user.uid,
        expires_in=data.get("expires_in", 0),
        multiplier=data.get("multiplier", 1),
    )
    return JsonResponse(
        {
            "result": 1,
            "message": "success",
            "data": {"id": getattr(entry, "id", None)},
        }
    )
