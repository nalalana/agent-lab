"""Operational APIs for the unified Agent runtime."""

from __future__ import annotations

import json

from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone as django_timezone
from django.utils.http import content_disposition_header

from apps.agent_runtime.alerts import evaluate_runtime_alerts
from apps.agent_runtime.knowledge.integrity import MigrationIntegrityReporter
from apps.agent_runtime.knowledge.workspace import (
    create_folder,
    delete_path,
    list_tree,
    open_file,
    upload_file,
)
from apps.agent_runtime.knowledge.services import KnowledgeIndexService, workspace_file_id
from apps.agent_runtime.management_api import (
    knowledge_file_queryset,
    memory_job_queryset,
    paginate,
    schedule_queryset,
    serialize_knowledge_file,
    serialize_memory_job,
    serialize_schedule,
)
from apps.agent_runtime.memory.tasks import requeue_memory_commit_job
from apps.agent_runtime.models import AgentRun, AgentSchedule, MemorySyncJob
from apps.agent_runtime.operations import list_company_runs, runtime_metrics, serialize_run_detail
from apps.agent_runtime.runtime import request_run_cancel
from apps.agent_runtime.schedules import (
    compute_next_run_at,
    ensure_schedule_session,
    initialize_next_run,
    reusable_schedule_run,
    run_due_schedule,
)
from apps.agent_runtime.serializers import (
    AgentRunDetailQuerySerializer,
    AgentRunListQuerySerializer,
    CompanyQuerySerializer,
    KnowledgeFolderCreateSerializer,
    KnowledgeFileContentQuerySerializer,
    KnowledgeFileListQuerySerializer,
    KnowledgePathDeleteSerializer,
    KnowledgeTreeQuerySerializer,
    KnowledgeUploadSerializer,
    MemoryJobListQuerySerializer,
    RuntimeAlertsQuerySerializer,
    RuntimeMetricsQuerySerializer,
    ScheduleCreateSerializer,
    ScheduleListQuerySerializer,
)
from apps.agent_runtime.tasks import run_agent_schedule
from apps.company.models import Company, TransparentCompanyAgentDefinition, TransparentCompanyAgentTestSession
from apps.company.admin_access import agent_access, file_allowed, filter_readable_tree, scope_for
from apps.company.permissions import is_company_verified_for_formal_access
from apps.iam.models import Grant
from apps.company.permissions import can_access_formal_company
from apps.company.services import CompanyService
from apps.runtime.models import WorkspaceFile
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


@login_checker
def run_list(request):
    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    serializer = AgentRunListQuerySerializer(data=request.GET)
    company_or_response = _authorized_company(request, serializer)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    data = list_company_runs(company_or_response, serializer.validated_data, agent_ids=request.runtime_agent_ids)
    return JsonResponse({"result": 1, "message": "查询成功", "data": data})


@login_checker
def run_detail(request, run_id):
    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    serializer = AgentRunDetailQuerySerializer(data=request.GET)
    company_or_response = _authorized_company(request, serializer)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    run = (
        AgentRun.objects.filter(company=company_or_response, pk=run_id)
        .filter(**({"agent__agentId__in": request.runtime_agent_ids} if request.runtime_agent_ids is not None else {}))
        .select_related("agent", "user", "session", "sandbox_execution")
        .prefetch_related("tool_calls", "events")
        .first()
    )
    if run is None:
        return JsonResponse({"result": 0, "message": "Run不存在"}, status=404)
    return JsonResponse(
        {"result": 1, "message": "查询成功", "data": serialize_run_detail(run)}
    )


@login_checker
def metrics(request):
    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    serializer = RuntimeMetricsQuerySerializer(data=request.GET)
    company_or_response = _authorized_company(request, serializer)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    return JsonResponse(
        {
            "result": 1,
            "message": "查询成功",
            "data": runtime_metrics(company_or_response, serializer.validated_data, agent_ids=request.runtime_agent_ids),
        }
    )

@login_checker
def runtime_alerts(request):
    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    serializer = RuntimeAlertsQuerySerializer(data=request.GET)
    company_or_response = _authorized_company(request, serializer)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    return JsonResponse(
        {
            "result": 1,
            "message": "查询成功",
            "data": evaluate_runtime_alerts(company_or_response, agent_ids=request.runtime_agent_ids),
        }
    )


def _authorized_company(request, serializer):
    """Resolve company through IAM; never trust cid as tenant authority."""

    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    user = get_user_from_request(request)
    if user is None:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)
    ok, company_or_message = CompanyService._get_company_for_admin(
        serializer.validated_data["cid"],
        user,
        request.META.get("REMOTE_ADDR"),
    )
    if ok:
        request.runtime_agent_ids = None
        return company_or_message
    access_ok, transparent_or_message, scope = agent_access(serializer.validated_data["cid"], user)
    if not access_ok or scope is None or not scope.agent_ids:
        return JsonResponse({"result": 0, "message": "无权查看运行监控"}, status=403)
    request.runtime_agent_ids = scope.agent_ids
    return transparent_or_message.company


@login_checker
def cancel_run(request, run_id):
    """Request cancellation for one company-scoped AgentRun."""

    if request.method != "POST":
        return _failure("仅支持POST请求", 405)

    user = get_user_from_request(request)
    if user is None:
        return _failure("请先登录", 401)

    run = AgentRun.objects.select_related("company").filter(pk=run_id).first()
    if run is None:
        return _failure("Run不存在", 404)

    ok, company_or_message = CompanyService._get_company_for_admin(
        run.company.cid,
        user,
        request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _failure(company_or_message, 403)
    if company_or_message.pk != run.company_id:
        return _failure("无权取消该Run", 403)

    result = request_run_cancel(run_id=run.pk, company=company_or_message)
    return _success(result.as_dict(), "取消请求已提交")

@login_checker
def schedule_list(request):
    if request.method == "GET":
        context = _admin_context(request, ScheduleListQuerySerializer, "GET")
        if context[2]:
            return context[2]
        company, filters, _error = context
        data = paginate(schedule_queryset(company, filters), filters, serialize_schedule)
        return _success(data)
    if request.method != "POST":
        return _failure("仅支持GET/POST请求", 405)
    content_type = str(request.content_type or "").lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _failure("请求体必须是有效的JSON", 400)
    else:
        payload = request.POST
    serializer = ScheduleCreateSerializer(data=payload)
    if not serializer.is_valid():
        return _failure(serializer.errors, 400)
    user = get_user_from_request(request)
    if user is None:
        return _failure("请先登录", 401)
    ok, company_or_message = CompanyService._get_company_for_admin(
        serializer.validated_data["cid"],
        user,
        request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return _failure(company_or_message, 403)
    company = company_or_message
    agent = (
        TransparentCompanyAgentDefinition.objects.select_related("transparentCompany")
        .filter(
            transparentCompany__company=company,
            agentId=serializer.validated_data["agent_id"],
            enabled=True,
        )
        .first()
    )
    if agent is None:
        return _failure("Agent不存在或不可用", 404)
    schedule = AgentSchedule.objects.create(
        company=company,
        agent=agent,
        operator=user,
        name=serializer.validated_data["name"],
        schedule_type=serializer.validated_data["schedule_type"],
        schedule_value=serializer.validated_data["schedule_value"],
        message=serializer.validated_data["message"],
        session_key=serializer.validated_data.get("session") or "",
        enabled=serializer.validated_data["enabled"],
        status=AgentSchedule.STATUS_ACTIVE,
    )
    if not schedule.session_key:
        schedule.session_key = f"schedule:{schedule.pk}"
        schedule.save(update_fields=["session_key", "updated_at"])
    try:
        initialize_next_run(schedule)
    except ValueError as exc:
        schedule.delete()
        return _failure(str(exc), 400)
    return _success(serialize_schedule(schedule), "定时任务已创建")


@login_checker
def run_schedule_now(request, schedule_id):
    context = _admin_context(request, CompanyQuerySerializer, "POST")
    if context[2]:
        return context[2]
    company, _filters, _error = context
    schedule = AgentSchedule.objects.filter(company=company, pk=schedule_id).first()
    if schedule is None:
        return _failure("定时任务不存在", 404)
    if not schedule.enabled or schedule.status != AgentSchedule.STATUS_ACTIVE:
        return _failure("定时任务已暂停，请先恢复后再运行", 409)
    now = django_timezone.now()
    session = ensure_schedule_session(schedule, now=now)
    previous_next_run_at = schedule.next_run_at
    AgentSchedule.objects.filter(pk=schedule.pk).update(next_run_at=now)
    if reusable_schedule_run(schedule) is not None:
        result = run_due_schedule(schedule, now=now)
        schedule.refresh_from_db()
        return _success(
            {
                "result": {**result.__dict__, "cache_hit": True},
                "schedule": serialize_schedule(schedule),
                "conversation_id": session.conversationId,
                "session_key": session.sessionKey,
            },
            "定时任务已执行",
        )
    try:
        async_result = run_agent_schedule.delay(str(schedule.pk))
    except Exception:
        AgentSchedule.objects.filter(pk=schedule.pk).update(next_run_at=previous_next_run_at)
        return _failure("定时任务投递失败，请稍后重试", 503)
    schedule.refresh_from_db()
    return _success(
        {
            "result": {
                "schedule_id": str(schedule.pk),
                "status": "queued",
                "task_id": async_result.id,
            },
            "schedule": serialize_schedule(schedule),
            "conversation_id": session.conversationId,
            "session_key": session.sessionKey,
        },
        "定时任务已进入执行队列",
    )


@login_checker
def acknowledge_schedule(request, schedule_id):
    context = _admin_context(request, CompanyQuerySerializer, "POST")
    if context[2]:
        return context[2]
    company, _filters, _error = context
    schedule = AgentSchedule.objects.filter(company=company, pk=schedule_id).first()
    if schedule is None:
        return _failure("定时任务不存在", 404)
    metadata = dict(schedule.metadata or {})
    if schedule.last_run_id:
        metadata["acknowledged_run_id"] = str(schedule.last_run_id)
    schedule.metadata = metadata
    schedule.save(update_fields=["metadata", "updated_at"])
    schedule.refresh_from_db()
    return _success(serialize_schedule(schedule), "已知晓定时任务")


@login_checker
def delete_schedule(request, schedule_id):
    if request.method not in {"GET", "DELETE", "PATCH"}:
        return _failure("仅支持GET/DELETE/PATCH请求", 405)
    context = _admin_context(request, CompanyQuerySerializer, request.method)
    if context[2]:
        return context[2]
    company, _filters, _error = context
    schedule = AgentSchedule.objects.filter(company=company, pk=schedule_id).first()
    if schedule is None:
        return _failure("定时任务不存在", 404)
    if request.method == "GET":
        return _success(serialize_schedule(schedule), "查询成功")
    if request.method == "PATCH":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _failure("请求体必须是有效的JSON", 400)
        if set(payload).issubset({"enabled"}):
            if "enabled" not in payload or not isinstance(payload["enabled"], bool):
                return _failure("enabled必须是布尔值", 400)
            enabled = payload["enabled"]
            schedule.enabled = enabled
            schedule.status = AgentSchedule.STATUS_ACTIVE if enabled else AgentSchedule.STATUS_PAUSED
            schedule.next_run_at = None
            schedule.retry_after = None
            schedule.save(update_fields=["enabled", "status", "next_run_at", "retry_after", "updated_at"])
            if enabled:
                try:
                    initialize_next_run(schedule)
                except ValueError as exc:
                    return _failure(str(exc), 400)
            schedule.refresh_from_db()
            return _success(serialize_schedule(schedule), "定时任务已恢复" if enabled else "定时任务已暂停")

        serializer = ScheduleCreateSerializer(data={
            **payload,
            "cid": company.cid,
            "enabled": schedule.enabled,
        })
        if not serializer.is_valid():
            return _failure(serializer.errors, 400)
        agent = (
            TransparentCompanyAgentDefinition.objects.select_related("transparentCompany")
            .filter(
                transparentCompany__company=company,
                agentId=serializer.validated_data["agent_id"],
                enabled=True,
            )
            .first()
        )
        if agent is None:
            return _failure("Agent不存在或不可用", 404)
        try:
            next_run_at = compute_next_run_at(
                serializer.validated_data["schedule_type"],
                serializer.validated_data["schedule_value"],
            ) if schedule.enabled else None
        except ValueError as exc:
            return _failure(str(exc), 400)
        schedule.agent = agent
        schedule.name = serializer.validated_data["name"]
        schedule.schedule_type = serializer.validated_data["schedule_type"]
        schedule.schedule_value = serializer.validated_data["schedule_value"]
        schedule.message = serializer.validated_data["message"]
        schedule.next_run_at = next_run_at
        schedule.retry_after = None
        schedule.save(update_fields=[
            "agent", "name", "schedule_type", "schedule_value", "message",
            "next_run_at", "retry_after", "updated_at",
        ])
        TransparentCompanyAgentTestSession.objects.filter(
            transparentCompany=agent.transparentCompany,
            sessionKey=schedule.session_key,
        ).update(agent=agent, title=schedule.name)
        schedule.refresh_from_db()
        return _success(serialize_schedule(schedule), "定时任务已更新")
    schedule.delete()
    return _success({"id": str(schedule_id)}, "定时任务已删除")


@login_checker
def memory_job_list(request):
    context = _admin_context(request, MemoryJobListQuerySerializer, "GET")
    if context[2]:
        return context[2]
    company, filters, _error = context
    data = paginate(memory_job_queryset(company, filters), filters, serialize_memory_job)
    return _success(data)


@login_checker
def retry_memory_job(request, job_id):
    context = _admin_context(request, CompanyQuerySerializer, "POST")
    if context[2]:
        return context[2]
    company, _filters, _error = context
    job = MemorySyncJob.objects.filter(company=company, pk=job_id).first()
    if job is None:
        return _failure("记忆任务不存在", 404)
    if job.job_type == MemorySyncJob.TYPE_KNOWLEDGE_INDEX:
        return _retry_knowledge_job(job)
    try:
        requeue_memory_commit_job(job)
    except ValueError as exc:
        return _failure(str(exc), 409)
    job.refresh_from_db()
    return _success(serialize_memory_job(job), "任务已重新入队")


@login_checker
def knowledge_file_list(request):
    context = _admin_context(request, KnowledgeFileListQuerySerializer, "GET")
    if context[2]:
        return context[2]
    company, filters, _error = context
    data = paginate(knowledge_file_queryset(company, filters), filters, serialize_knowledge_file)
    return _success(data)


@login_checker
def reindex_knowledge_file(request, file_id):
    context = _admin_context(request, CompanyQuerySerializer, "POST")
    if context[2]:
        return context[2]
    company, _filters, _error = context
    workspace_file = WorkspaceFile.objects.filter(company=company, pk=file_id).first()
    if workspace_file is None:
        return _failure("知识文件不存在", 404)
    try:
        job = KnowledgeIndexService().enqueue(workspace_file, force=True)
    except ValueError as exc:
        return _failure(str(exc), 400)
    workspace_file.refresh_from_db()
    return _success(
        {"job": serialize_memory_job(job), "file": serialize_knowledge_file(workspace_file)},
        "索引任务已入队",
    )


@login_checker
def knowledge_tree(request):
    if request.method != 'GET':
        return _failure('仅支持GET请求', 405)
    serializer = KnowledgeTreeQuerySerializer(data=request.GET)
    company_or_response = _knowledge_company(request, serializer, 'read', allow_ancestor=True)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    company, filters = company_or_response, serializer.validated_data
    try:
        return _success(
            filter_readable_tree(company, get_user_from_request(request), list_tree(
                company,
                logical_path=filters["logical_path"],
                recursive=filters["recursive"],
            ))
        )
    except ValueError as exc:
        return _failure(str(exc), 400)


@login_checker
def knowledge_folder_create(request):
    payload = request.POST or request.GET
    serializer = KnowledgeFolderCreateSerializer(data=payload)
    company_or_response = _knowledge_company(request, serializer, 'write')
    if request.method != "POST":
        return _failure("仅支持POST请求", 405)
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    user = get_user_from_request(request)
    try:
        data = create_folder(
            company_or_response,
            serializer.validated_data["logical_path"],
            getattr(user, "uid", ""),
        )
    except ValueError as exc:
        return _failure(str(exc), 400)
    return _success(data, "文件夹已创建")


@login_checker
def knowledge_path_delete(request):
    if request.method != 'DELETE':
        return _failure('仅支持DELETE请求', 405)
    serializer = KnowledgePathDeleteSerializer(data=request.GET)
    company_or_response = _knowledge_company(request, serializer, 'delete')
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    company, filters = company_or_response, serializer.validated_data
    try:
        return _success(delete_path(company, filters["logical_path"]), "路径已删除")
    except ValueError as exc:
        return _failure(str(exc), 400)
    except Exception:
        return _failure("删除知识库文件失败", 502)


def _stream_cos_body(body, chunk_size=64 * 1024):
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


@login_checker
def knowledge_file_content(request):
    if request.method != 'GET':
        return _failure('仅支持GET请求', 405)
    serializer = KnowledgeFileContentQuerySerializer(data=request.GET)
    company_or_response = _knowledge_company(request, serializer, 'read')
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    company, filters = company_or_response, serializer.validated_data
    try:
        workspace_file, body = open_file(company, filters["logical_path"])
    except FileNotFoundError as exc:
        return _failure(str(exc), 404)
    except ValueError as exc:
        return _failure(str(exc), 400)
    except Exception:
        return _failure("读取知识文件失败", 502)

    response = StreamingHttpResponse(
        _stream_cos_body(body),
        content_type=workspace_file.mime_type or "application/octet-stream",
    )
    if workspace_file.size:
        response["Content-Length"] = str(workspace_file.size)
    response["Content-Disposition"] = content_disposition_header(
        False,
        workspace_file.file_name or filters["logical_path"].rsplit("/", 1)[-1],
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_checker
def knowledge_upload(request):
    if request.method != "POST":
        return _failure("仅支持POST请求", 405)
    payload = request.POST.copy()
    payload["file"] = request.FILES.get("file")
    serializer = KnowledgeUploadSerializer(data=payload)
    company_or_response = _knowledge_company(request, serializer, 'write')
    if isinstance(company_or_response, JsonResponse):
        return company_or_response
    user = get_user_from_request(request)
    try:
        data = upload_file(
            company_or_response,
            serializer.validated_data["file"],
            serializer.validated_data["logical_path"],
            getattr(user, "uid", ""),
        )
    except ValueError as exc:
        return _failure(str(exc), 400)
    except Exception:
        return _failure("上传知识文件失败", 502)
    return _success(data, "文件已上传并进入索引队列")


def _knowledge_company(request, serializer, action, allow_ancestor=False):
    if not serializer.is_valid():
        return _failure(serializer.errors, 400)
    user = get_user_from_request(request)
    if user is None:
        return _failure('请先登录', 401)
    company = Company.objects.filter(cid=serializer.validated_data['cid']).first()
    if not company or not is_company_verified_for_formal_access(company):
        return _failure('无权访问或企业不存在', 403)
    path = serializer.validated_data['logical_path']
    if file_allowed(company, user, action, path):
        return company
    if allow_ancestor and action == 'read':
        from apps.agent_runtime.knowledge.workspace import normalize_path
        try:
            normalized = normalize_path(path)
        except ValueError:
            return _failure('知识库路径不合法', 400)
        scope = scope_for(company, user)
        if scope and any(
            'read' in rule.get('actions', []) and rule.get('path', '').startswith(f'{normalized}/')
            for rule in scope.file_rules if isinstance(rule, dict)
        ):
            return company
    return _failure('无权访问该文件路径', 403)


@login_checker
def migration_integrity(request):
    context = _admin_context(request, CompanyQuerySerializer, "GET")
    if context[2]:
        return context[2]
    company, _filters, _error = context
    return _success(MigrationIntegrityReporter().build(company=company))


def _retry_knowledge_job(job):
    try:
        workspace_file = WorkspaceFile.objects.get(
            company_id=job.company_id,
            pk=workspace_file_id(job.source_ref),
        )
        queued = KnowledgeIndexService().enqueue(workspace_file, force=True)
    except (WorkspaceFile.DoesNotExist, ValueError) as exc:
        return _failure(str(exc), 409)
    return _success(serialize_memory_job(queued), "任务已重新入队")


def _admin_context(request, serializer_class, method):
    if request.method != method:
        return None, None, _failure(f"仅支持{method}请求", 405)
    serializer = serializer_class(data=request.GET)
    if not serializer.is_valid():
        return None, None, _failure(serializer.errors, 400)
    user = get_user_from_request(request)
    if user is None:
        return None, None, _failure("请先登录", 401)
    ok, company_or_message = CompanyService._get_company_for_admin(
        serializer.validated_data["cid"],
        user,
        request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return None, None, _failure(company_or_message, 403)
    return company_or_message, serializer.validated_data, None


def _success(data, message="查询成功"):
    return JsonResponse({"result": 1, "message": message, "data": data})


def _failure(message, status):
    return JsonResponse({"result": 0, "message": message}, status=status)
