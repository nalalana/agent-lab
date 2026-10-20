import json

from django.http import JsonResponse

from apps.company.models import Company
from apps.runtime.models import OpenClawInstance, OpenClawProvisionTask
from apps.runtime.serializers import (
    RuntimeAgentWorkspaceToolExecuteSerializer,
    RuntimeAgentExportsInspectionSerializer,
    RuntimeAuditLogListSerializer,
    RuntimeBootstrapSerializer,
    RuntimeConfigDiffSerializer,
    RuntimeConfigPreviewSerializer,
    RuntimeConfigPublishSerializer,
    RuntimeConfigReadbackSerializer,
    RuntimeConfigVersionListSerializer,
    RuntimeCronJobActionSerializer,
    RuntimeCronJobCreateSerializer,
    RuntimeCronJobListSerializer,
    RuntimeCronRunListSerializer,
    RuntimeDeployBridgeSerializer,
    RuntimeAlertPolicyDetailSerializer,
    RuntimeAlertPolicyUpsertSerializer,
    RuntimeDriftEventListSerializer,
    RuntimeDriftEventResolveSerializer,
    RuntimeGatewayStateSerializer,
    RuntimeHealthCheckSerializer,
    RuntimeInstanceDetailSerializer,
    RuntimeLighthouseCatalogSerializer,
    RuntimeLighthousePurchaseDryRunSerializer,
    RuntimeProvisionSerializer,
    RuntimeRolloutPolicyDetailSerializer,
    RuntimeRolloutPolicyUpsertSerializer,
    RuntimeServerResourceListSerializer,
    RuntimeSnapshotActionSerializer,
    RuntimeSnapshotListSerializer,
    RuntimeTaskListSerializer,
    RuntimeWorkspaceDownloadTicketSerializer,
    RuntimeWorkspaceFileDeleteSerializer,
    RuntimeWorkspaceFileDetailSerializer,
    RuntimeWorkspaceFileListSerializer,
    RuntimeWorkspaceFileRenameSerializer,
    RuntimeWorkspacePathRenameSerializer,
    RuntimeWorkspacePathSerializer,
    RuntimeWorkspaceTransferTaskListSerializer,
    RuntimeWorkspaceUploadFileSerializer,
    RuntimeWorkspaceUploadTicketSerializer,
)
from apps.runtime.services import (
    AgentWorkspaceToolGatewayService,
    OpenClawConfigService,
    OpenClawRuntimeService,
    RuntimeGovernanceService,
)
from apps.risk_control.services import RiskControlService
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


class InvalidJsonBody(ValueError):
    pass


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidJsonBody("request body must be a valid JSON object") from exc
    if not isinstance(data, dict):
        raise InvalidJsonBody("request body must be a JSON object")
    return data


def _invalid_json_response(exc: InvalidJsonBody) -> JsonResponse:
    return JsonResponse({"result": 0, "message": str(exc)}, status=400)


def runtime_legacy_retired(_request):
    """Stable tombstone for Phase 4-C retired infrastructure workflows."""

    return JsonResponse(
        {
            "schemaVersion": 1,
            "result": 0,
            "message": "OpenClaw旧基础设施流程已下线",
            "data": {
                "retired": True,
                "replacement": "/api/agent-runtime",
            },
        },
        status=410,
    )


def _load_company(cid: str) -> tuple[bool, Company | str]:
    try:
        return True, Company.objects.get(cid=cid)
    except Company.DoesNotExist:
        return False, "company not found"


def _require_admin_company(request, cid: str):
    user = get_user_from_request(request)
    if not user:
        return False, JsonResponse({"result": 0, "message": "login required"}, status=401)

    ok, company_or_msg = _load_company(cid)
    if not ok:
        return False, JsonResponse({"result": 0, "message": company_or_msg}, status=404)
    company = company_or_msg

    ok, message = OpenClawRuntimeService.assert_company_admin(company, user)
    if not ok:
        return False, JsonResponse({"result": 0, "message": message}, status=403)

    return True, (company, user)


@login_checker
def runtime_config_preview(request):
    serializer = RuntimeConfigPreviewSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, config_or_msg = OpenClawConfigService.build_company_preview(company)
    if not ok:
        return JsonResponse({"result": 0, "message": config_or_msg}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": config_or_msg})


@login_checker
def runtime_config_version_list(request):
    serializer = RuntimeConfigVersionListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    data = OpenClawConfigService.list_company_versions(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_workspace_file_list(request):
    serializer = RuntimeWorkspaceFileListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    data = OpenClawRuntimeService.list_workspace_files(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_workspace_file_detail(request):
    serializer = RuntimeWorkspaceFileDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, result = OpenClawRuntimeService.read_workspace_file(
        company=company,
        file_id=serializer.validated_data["file_id"],
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_workspace_file_delete(request):
    serializer = RuntimeWorkspaceFileDeleteSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.delete_workspace_file(
        company=company,
        file_id=serializer.validated_data["file_id"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace file deleted", "data": result})


@login_checker
def runtime_workspace_file_rename(request):
    serializer = RuntimeWorkspaceFileRenameSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.rename_workspace_file(
        company=company,
        file_id=serializer.validated_data["file_id"],
        new_logical_path=serializer.validated_data["new_logical_path"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace file renamed", "data": result})


@login_checker
def runtime_workspace_path_detail(request):
    serializer = RuntimeWorkspacePathSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, result = OpenClawRuntimeService.read_workspace_logical_path(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_workspace_path_create(request):
    serializer = RuntimeWorkspacePathSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.create_workspace_logical_path(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace path created", "data": result})


@login_checker
def runtime_workspace_path_delete(request):
    serializer = RuntimeWorkspacePathSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.delete_workspace_logical_path(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace path deleted", "data": result})


@login_checker
def runtime_workspace_path_rename(request):
    serializer = RuntimeWorkspacePathRenameSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.rename_workspace_logical_path(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
        new_logical_path=serializer.validated_data["new_logical_path"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace path renamed", "data": result})


@login_checker
def runtime_workspace_transfer_task_list(request):
    serializer = RuntimeWorkspaceTransferTaskListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    data = OpenClawRuntimeService.list_workspace_transfer_tasks(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_workspace_upload_ticket(request):
    serializer = RuntimeWorkspaceUploadTicketSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.issue_workspace_upload_ticket(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
        file_name=serializer.validated_data["file_name"],
        mime_type=serializer.validated_data.get("mime_type", ""),
        size=serializer.validated_data.get("size", 0) or 0,
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "upload ticket issued", "data": result})


@login_checker
def runtime_workspace_upload_file(request):
    data = request.POST.copy()
    if request.FILES.get("file") is not None:
        data["file"] = request.FILES["file"]
    serializer = RuntimeWorkspaceUploadFileSerializer(data=data)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    uploaded_file = serializer.validated_data["file"]
    file_name = (
        serializer.validated_data.get("file_name")
        or getattr(uploaded_file, "name", "")
        or serializer.validated_data["logical_path"].split("/")[-1]
    )
    mime_type = serializer.validated_data.get("mime_type") or getattr(uploaded_file, "content_type", "") or ""
    size = serializer.validated_data.get("size") or getattr(uploaded_file, "size", 0) or 0

    ok, result = OpenClawRuntimeService.upload_workspace_file(
        company=company,
        logical_path=serializer.validated_data["logical_path"],
        file_name=file_name,
        file_content=uploaded_file.read(),
        mime_type=mime_type,
        size=size,
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "workspace file uploaded", "data": result})


@login_checker
def runtime_workspace_download_ticket(request):
    serializer = RuntimeWorkspaceDownloadTicketSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    ok, result = OpenClawRuntimeService.issue_workspace_download_ticket(
        company=company,
        file_id=serializer.validated_data["file_id"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "download ticket issued", "data": result})


@login_checker
def runtime_config_publish(request):
    serializer = RuntimeConfigPublishSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="config.publish",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, version_or_msg = OpenClawConfigService.create_config_version(
        company=company,
        operator_id=user.uid,
        notes=serializer.validated_data.get("notes", ""),
        publish=True,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": version_or_msg}, status=400)
    return JsonResponse({"result": 1, "message": "config published", "data": version_or_msg})


@login_checker
def runtime_config_publish_and_verify(request):
    serializer = RuntimeConfigPublishSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="config.publish_and_verify",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, version_or_msg = OpenClawConfigService.create_config_version(
        company=company,
        operator_id=user.uid,
        notes=serializer.validated_data.get("notes", ""),
        publish=True,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": version_or_msg}, status=400)

    result = {"version": version_or_msg, "verified": False}
    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if ok:
        instance = instance_or_msg
        bootstrap_ok, bootstrap_result = OpenClawRuntimeService.start_bootstrap(company, instance)
        if bootstrap_ok:
            bootstrap_task = OpenClawProvisionTask.objects.filter(
                id=(bootstrap_result.get("task") or {}).get("id")
            ).first()
            if bootstrap_task:
                run_ok, run_result = OpenClawRuntimeService.run_bootstrap(bootstrap_task)
                result["bootstrap"] = run_result if run_ok else {"error": run_result}
            else:
                result["bootstrap"] = {"error": "bootstrap task missing"}
        else:
            result["bootstrap"] = {"error": bootstrap_result}
        readback_ok, readback_payload = OpenClawRuntimeService.readback_config(instance)
        diff_ok, diff_payload = OpenClawConfigService.build_company_config_diff(company)
        result["readback"] = readback_payload
        result["diff"] = diff_payload if diff_ok else {"error": diff_payload}
        result["verified"] = bool(
            bootstrap_ok
            and readback_ok
            and diff_ok
            and isinstance(diff_payload, dict)
            and bool(diff_payload.get("is_in_sync"))
        )
        instance.refresh_from_db()
        result["instance"] = instance.get_info()
    else:
        result["readback"] = {"error": instance_or_msg}
        result["diff"] = {"error": instance_or_msg}

    return JsonResponse({"result": 1, "message": "config published and verified", "data": result})


@login_checker
def runtime_config_readback(request):
    serializer = RuntimeConfigReadbackSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.enqueue_config_readback(instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "config readback submitted", "data": result})


@login_checker
def runtime_config_diff(request):
    serializer = RuntimeConfigDiffSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, result = OpenClawConfigService.build_company_config_diff(company)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_gateway_state(request):
    serializer = RuntimeGatewayStateSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.get_gateway_state(instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_cron_job_list(request):
    serializer = RuntimeCronJobListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({
            "result": 1,
            "message": "ok",
            "data": {"total": 0, "items": [], "hasMore": False},
        })

    ok, result = OpenClawRuntimeService.list_cron_jobs(instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_cron_run_list(request):
    serializer = RuntimeCronRunListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.list_cron_runs(
        instance_or_msg,
        serializer.validated_data["id"],
        limit=serializer.validated_data["limit"],
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_cron_job_create(request):
    serializer = RuntimeCronJobCreateSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    bridge_payload = {
        key: value
        for key, value in serializer.validated_data.items()
        if key != "cid"
    }
    ok, result = OpenClawRuntimeService.create_cron_job(instance_or_msg, bridge_payload)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_cron_job_run(request):
    serializer = RuntimeCronJobActionSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=_user.id,
        company_id=company.cid,
        resource_id=f"cron.run:{serializer.validated_data['id']}",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.run_cron_job(instance_or_msg, serializer.validated_data["id"])
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_cron_job_remove(request):
    serializer = RuntimeCronJobActionSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.remove_cron_job(instance_or_msg, serializer.validated_data["id"])
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_agent_workspace_tool_execute(request):
    serializer = RuntimeAgentWorkspaceToolExecuteSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=_user.id,
        company_id=company.cid,
        agent_id=serializer.validated_data["agent_id"],
        resource_id=f"workspace_tool:{serializer.validated_data['tool']}",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, result = AgentWorkspaceToolGatewayService.execute(
        company,
        agent_id=serializer.validated_data["agent_id"],
        tool_name=serializer.validated_data["tool"],
        arguments=serializer.validated_data.get("arguments") or {},
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=403)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_rollout_policy_detail(request):
    serializer = RuntimeRolloutPolicyDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    return JsonResponse({"result": 1, "message": "ok", "data": RuntimeGovernanceService.get_or_create_rollout_policy(company).get_info()})


@login_checker
def runtime_rollout_policy_upsert(request):
    serializer = RuntimeRolloutPolicyUpsertSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload
    data = RuntimeGovernanceService.upsert_rollout_policy(company, serializer.validated_data, operator_id=user.uid)
    return JsonResponse({"result": 1, "message": "rollout policy updated", "data": data})


@login_checker
def runtime_alert_policy_detail(request):
    serializer = RuntimeAlertPolicyDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    return JsonResponse({"result": 1, "message": "ok", "data": RuntimeGovernanceService.get_or_create_alert_policy(company).get_info()})


@login_checker
def runtime_alert_policy_upsert(request):
    serializer = RuntimeAlertPolicyUpsertSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload
    data = RuntimeGovernanceService.upsert_alert_policy(company, serializer.validated_data, operator_id=user.uid)
    return JsonResponse({"result": 1, "message": "alert policy updated", "data": data})


@login_checker
def runtime_drift_event_list(request):
    serializer = RuntimeDriftEventListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    data = RuntimeGovernanceService.list_drift_events(
        company=company,
        status=serializer.validated_data.get("status", ""),
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_drift_event_resolve(request):
    serializer = RuntimeDriftEventResolveSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload
    ok, result = RuntimeGovernanceService.resolve_drift_event(
        company=company,
        event_id=serializer.validated_data["event_id"],
        resolution=serializer.validated_data["resolution"],
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "drift event resolved", "data": result})


@login_checker
def runtime_instance_detail(request):
    serializer = RuntimeInstanceDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, company_or_msg = _load_company(serializer.validated_data["cid"])
    if not ok:
        return JsonResponse({"result": 0, "message": company_or_msg}, status=404)
    company = company_or_msg

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        placeholder = {
            "id": None,
            "company_id": company.cid,
            "provider": OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
            "region": "",
            "zone": "",
            "instance_id": "",
            "instance_name": "",
            "host_id": "",
            "runtime_namespace": f"tsds-{company.cid[:12]}",
            "gateway_endpoint": "",
            "gateway_port": 18789,
            "config_path": "",
            "workspace_prefix": "",
            "workspace_mode": OpenClawInstance.WORKSPACE_MODE_COS_BACKED,
            "connection_mode": OpenClawInstance.CONNECTION_MODE_AGENT,
            "public_ip": "",
            "private_ip": "",
            "status": OpenClawInstance.STATUS_PENDING,
            "openclaw_status": OpenClawInstance.OPENCLAW_STATUS_STARTING,
            "config_status": OpenClawInstance.CONFIG_STATUS_PENDING,
            "deploy_version": "",
            "current_publish_version": "",
            "current_runtime_version": "",
            "create_source": "upgrade_pending",
            "last_health_check_at": None,
            "last_readback_at": None,
            "last_error": "",
            "created_at": None,
            "updated_at": None,
            "exists": False,
            "message": str(instance_or_msg),
        }
        return JsonResponse({"result": 1, "message": "runtime pending", "data": placeholder})
    return JsonResponse({"result": 1, "message": "ok", "data": instance_or_msg.get_info()})


@login_checker
def runtime_agent_exports_inspection(request):
    serializer = RuntimeAgentExportsInspectionSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, result = OpenClawRuntimeService.inspect_agent_runtime_exports(company)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_task_list(request):
    serializer = RuntimeTaskListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, company_or_msg = _load_company(serializer.validated_data["cid"])
    if not ok:
        return JsonResponse({"result": 0, "message": company_or_msg}, status=404)
    company = company_or_msg

    data = OpenClawRuntimeService.list_company_tasks(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_audit_log_list(request):
    serializer = RuntimeAuditLogListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, company_or_msg = _load_company(serializer.validated_data["cid"])
    if not ok:
        return JsonResponse({"result": 0, "message": company_or_msg}, status=404)
    company = company_or_msg

    data = OpenClawRuntimeService.list_company_audit_logs(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_lighthouse_catalog(request):
    serializer = RuntimeLighthouseCatalogSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="lighthouse.catalog",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, result = OpenClawRuntimeService.list_lighthouse_purchase_catalog(
        region=serializer.validated_data.get("region", ""),
        zone=serializer.validated_data.get("zone", ""),
        blueprint_type=serializer.validated_data.get("blueprint_type", ""),
        platform_type=serializer.validated_data.get("platform_type", "LINUX_UNIX"),
        keyword=serializer.validated_data.get("keyword", ""),
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "ok", "data": result})


@login_checker
def runtime_lighthouse_purchase_dry_run(request):
    try:
        request_body = _json_body(request)
    except InvalidJsonBody as exc:
        return _invalid_json_response(exc)

    serializer = RuntimeLighthousePurchaseDryRunSerializer(data=request_body)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="lighthouse.purchase_dry_run",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    request_payload = {
        key: serializer.validated_data.get(key, "")
        for key in [
            "region",
            "zone",
            "bundle_id",
            "blueprint_id",
            "period",
            "renew_flag",
            "firewall_template_id",
            "key_id",
            "client_token",
        ]
        if serializer.validated_data.get(key, "") not in (None, "")
    }
    ok, result = OpenClawRuntimeService.dry_run_lighthouse_purchase(
        company=company,
        request_payload=request_payload,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "lighthouse purchase dry-run passed", "data": result})


@login_checker
def runtime_instance_provision(request):
    serializer = RuntimeProvisionSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="instance.provision",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    provision_payload = {
        key: serializer.validated_data.get(key, "")
        for key in [
            "instance_id",
            "instance_name",
            "public_ip",
            "private_ip",
            "expired_at",
            "bundle_id",
            "blueprint_id",
            "period",
            "renew_flag",
            "firewall_template_id",
            "key_id",
            "client_token",
            "dry_run",
        ]
        if serializer.validated_data.get(key, "") not in (None, "")
    }
    ok, result = OpenClawRuntimeService.enqueue_instance_provision(
        company=company,
        operator_id=user.uid,
        provider=serializer.validated_data.get("provider", ""),
        region=serializer.validated_data.get("region", ""),
        zone=serializer.validated_data.get("zone", ""),
        request_payload=provision_payload,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "runtime provision submitted", "data": result})



@login_checker
def runtime_instance_retry_provision(request):
    serializer = RuntimeBootstrapSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="instance.retry_provision",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, result = OpenClawRuntimeService.retry_paid_company_runtime_provision(
        company=company,
        operator_id=user.uid,
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "runtime provision retry submitted", "data": result})
@login_checker
def runtime_instance_bootstrap(request):
    serializer = RuntimeBootstrapSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=_user.id,
        company_id=company.cid,
        resource_id="instance.bootstrap",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.enqueue_bootstrap(company=company, instance=instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "openclaw bootstrap submitted", "data": result})




@login_checker
def runtime_instance_deploy_bridge(request):
    serializer = RuntimeDeployBridgeSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, user = payload

    risk_decision = RiskControlService.evaluate(
        "runtime.openclaw.call",
        request,
        actor_user_id=user.id,
        company_id=company.cid,
        resource_id="instance.deploy_bridge",
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.enqueue_lighthouse_bridge_deploy(
        company=company,
        instance=instance_or_msg,
        request_payload={
            "install_docker": serializer.validated_data.get("install_docker", False),
            "auto_bootstrap": serializer.validated_data.get("auto_bootstrap", True),
        },
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "lighthouse bridge deploy submitted", "data": result})
@login_checker
def runtime_instance_health_check(request):
    serializer = RuntimeHealthCheckSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)

    ok, result = OpenClawRuntimeService.enqueue_health_check(instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "health check submitted", "data": result})

@login_checker
def runtime_server_resource_list(request):
    serializer = RuntimeServerResourceListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload

    data = OpenClawRuntimeService.list_server_resources(
        company=company,
        provider=serializer.validated_data.get("provider", ""),
        status=serializer.validated_data.get("status", ""),
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_snapshot_list(request):
    serializer = RuntimeSnapshotListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    data = OpenClawRuntimeService.list_runtime_snapshots(
        company=company,
        limit=serializer.validated_data["limit"],
        offset=serializer.validated_data["offset"],
    )
    return JsonResponse({"result": 1, "message": "ok", "data": data})


@login_checker
def runtime_snapshot_backup(request):
    serializer = RuntimeSnapshotActionSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)
    ok, result = OpenClawRuntimeService.backup_runtime_state(instance_or_msg)
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "runtime snapshot created", "data": result})


@login_checker
def runtime_snapshot_restore(request):
    serializer = RuntimeSnapshotActionSerializer(data=_json_body(request))
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)
    ok, payload = _require_admin_company(request, serializer.validated_data["cid"])
    if not ok:
        return payload
    company, _user = payload
    ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
    if not ok:
        return JsonResponse({"result": 0, "message": instance_or_msg}, status=404)
    ok, result = OpenClawRuntimeService.restore_runtime_state(
        instance_or_msg,
        snapshot_id=serializer.validated_data.get("snapshot_id"),
    )
    if not ok:
        return JsonResponse({"result": 0, "message": result}, status=400)
    return JsonResponse({"result": 1, "message": "runtime snapshot restored", "data": result})
