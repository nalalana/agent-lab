"""Authenticated B2B console endpoint for OpenViking memory metadata."""

from __future__ import annotations

from django.core.cache import cache
from django.http import JsonResponse

from apps.agent_runtime.models import MemorySyncJob
from apps.agent_runtime.memory.bank import MemoryBankService
from apps.agent_runtime.memory.errors import MemoryGatewayError
from apps.company.services import CompanyService
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


# Cache entries are already versioned by the newest completed memory job. A
# longer TTL avoids repeating the expensive OpenViking scan when the data has
# not changed; a new completed job immediately produces a different key.
CONSOLE_MEMORY_CACHE_SECONDS = 24 * 60 * 60
TRANSIENT_MEMORY_WARNING_MARKERS = (
    "tenant concurrency limit exceeded",
    "tenant rate limit exceeded",
    "tenant limiter is unavailable",
    "temporarily unavailable",
)


def _console_memory_cache_key(company_id) -> str:
    """Version cached snapshots with the newest durable memory commit."""

    latest_job_id = (
        MemorySyncJob.objects.filter(
            company_id=company_id,
            job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
            status=MemorySyncJob.STATUS_COMPLETED,
        )
        .order_by("-completed_at", "-created_at")
        .values_list("pk", flat=True)
        .first()
    )
    version = str(latest_job_id) if latest_job_id else "empty"
    return f"console-memory:v4:{company_id}:{version}"


def _is_cacheable_memory_payload(payload) -> bool:
    warnings = payload.get("warnings") if isinstance(payload, dict) else None
    warning_text = "\n".join(str(value) for value in (warnings or ())).casefold()
    return not any(marker in warning_text for marker in TRANSIENT_MEMORY_WARNING_MARKERS)


@login_checker
def console_memory(request):
    """Return current long-term memories extracted by native OpenViking commits."""

    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)
    cid = str(request.GET.get("cid") or "").strip()
    if not cid:
        return JsonResponse({"result": 0, "message": "cid不能为空"}, status=400)

    ok, transparent_company_or_message = CompanyService._get_transparent_company_for_admin(
        cid=cid,
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return JsonResponse({"result": 0, "message": transparent_company_or_message})
    transparent_company = transparent_company_or_message
    cache_key = _console_memory_cache_key(transparent_company.company_id)
    try:
        payload = cache.get(cache_key)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        try:
            payload = MemoryBankService().list_company(
                transparent_company.company,
                transparent_company,
            )
        except MemoryGatewayError:
            return JsonResponse(
                {"result": 0, "message": "OpenViking长期记忆暂时不可用"},
                status=502,
            )
        if _is_cacheable_memory_payload(payload):
            try:
                cache.set(cache_key, payload, timeout=CONSOLE_MEMORY_CACHE_SECONDS)
            except Exception:
                pass
    return JsonResponse(
        {
            "result": 1,
            "message": "查询成功",
            "data": payload,
        }
    )
