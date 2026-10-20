import json

from django.http import JsonResponse, StreamingHttpResponse

from apps.company.services import CompanyService
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


INVALID_JSON_MESSAGE = "请求体必须是有效的 JSON 对象"
RESOURCE_UNAVAILABLE_MESSAGE = "无权访问或资源不存在"
_RESOURCE_DISCLOSURE_ERRORS = {
    "公司不存在",
    "透明公司不存在",
    "Agent不存在",
    "Agent 不存在",
    "测试会话不存在",
    "工作会话不存在",
}


def _parse_json_object(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JsonResponse(
            {"result": 0, "message": INVALID_JSON_MESSAGE},
            status=400,
        )
    if not isinstance(payload, dict):
        return None, JsonResponse(
            {"result": 0, "message": INVALID_JSON_MESSAGE},
            status=400,
        )
    return payload, None


def _service_error_message(value):
    if str(value or "").strip() in _RESOURCE_DISCLOSURE_ERRORS:
        return RESOURCE_UNAVAILABLE_MESSAGE
    return value


def _forbidden_response(value):
    return JsonResponse(
        {"result": 0, "message": _service_error_message(value)},
        status=403,
    )


def _sse_encode(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@login_checker
def transparent_company_agent_test_agents(request):
    from apps.company.serializers import TransparentCompanyAgentTestAgentsSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    serializer = TransparentCompanyAgentTestAgentsSerializer(
        data={"cid": request.GET.get("cid") or request.GET.get("company_id")}
    )
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, data = CompanyService.list_transparent_company_test_agents(
        cid=serializer.validated_data["cid"],
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(data)
    return JsonResponse({"result": 1, "message": "查询成功", "data": data})


@login_checker
def transparent_company_agent_test_sessions(request):
    from apps.company.serializers import TransparentCompanyAgentTestSessionsSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    serializer = TransparentCompanyAgentTestSessionsSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, data = CompanyService.list_transparent_company_test_sessions(
        cid=serializer.validated_data["cid"],
        agent_id=serializer.validated_data.get("agent_id") or "",
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(data)
    return JsonResponse({"result": 1, "message": "查询成功", "data": data})


@login_checker
def transparent_company_agent_test_session_create(request):
    from apps.company.serializers import TransparentCompanyAgentTestSessionCreateSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    payload, error_response = _parse_json_object(request)
    if error_response is not None:
        return error_response
    serializer = TransparentCompanyAgentTestSessionCreateSerializer(
        data=payload
    )
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, data = CompanyService.create_transparent_company_test_session(
        cid=serializer.validated_data["cid"],
        agent_id=serializer.validated_data["agent_id"],
        user=user,
        title=serializer.validated_data.get("title") or "",
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(data)
    return JsonResponse({"result": 1, "message": "创建成功", "data": data})


@login_checker
def transparent_company_agent_test_messages(request):
    from apps.company.serializers import TransparentCompanyAgentTestMessagesSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    serializer = TransparentCompanyAgentTestMessagesSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, data = CompanyService.get_transparent_company_test_messages(
        cid=serializer.validated_data["cid"],
        conversation_id=serializer.validated_data.get("conversation_id") or "",
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(data)
    return JsonResponse({"result": 1, "message": "查询成功", "data": data})


@login_checker
def transparent_company_agent_test_message_send(request):
    from apps.company.serializers import TransparentCompanyAgentTestMessageSendSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    payload, error_response = _parse_json_object(request)
    if error_response is not None:
        return error_response
    serializer = TransparentCompanyAgentTestMessageSendSerializer(
        data=payload
    )
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, data = CompanyService.send_transparent_company_test_message(
        cid=serializer.validated_data["cid"],
        conversation_id=serializer.validated_data.get("conversation_id") or "",
        content=serializer.validated_data["content"],
        agent_id=serializer.validated_data.get("agent_id") or "",
        title=serializer.validated_data.get("title") or "",
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(data)
    return JsonResponse({"result": 1, "message": "发送成功", "data": data})


@login_checker
def transparent_company_agent_test_message_stream(request):
    from apps.company.serializers import TransparentCompanyAgentTestMessageSendSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)

    payload, error_response = _parse_json_object(request)
    if error_response is not None:
        return error_response
    serializer = TransparentCompanyAgentTestMessageSendSerializer(
        data=payload
    )
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    try:
        from apps.agent_runtime.attachments import validate_chat_attachments
        attachments = validate_chat_attachments(serializer.validated_data.get("attachments"))
    except ValueError as exc:
        return JsonResponse({"result": 0, "message": str(exc)}, status=400)

    ok, stream_or_error = CompanyService.stream_transparent_company_test_message(
        cid=serializer.validated_data["cid"],
        conversation_id=serializer.validated_data.get("conversation_id") or "",
        content=serializer.validated_data.get("content") or "",
        agent_id=serializer.validated_data.get("agent_id") or "",
        title=serializer.validated_data.get("title") or "",
        attachments=attachments,
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _forbidden_response(stream_or_error)

    def event_stream():
        yield ": connected\n\n"
        for item in stream_or_error:
            if not isinstance(item, dict):
                continue
            event_name = str(item.get("event") or "message")
            payload = item.get("data")
            if not isinstance(payload, dict):
                payload = {
                    key: value
                    for key, value in item.items()
                    if key != "event"
                }
            yield _sse_encode(event_name, payload)

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
