from rest_framework import serializers

from apps.runtime.security import validate_workspace_logical_path
from django.conf import settings


def _validate_workspace_logical_path(value: str) -> str:
    try:
        return validate_workspace_logical_path(value, "logical_path")
    except ValueError as exc:
        raise serializers.ValidationError(str(exc)) from exc


def _validate_workspace_file_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise serializers.ValidationError("file_name is invalid")
    return normalized


class RuntimeInstanceDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeAgentExportsInspectionSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeConfigPreviewSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeServerResourceListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    provider = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeSnapshotListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeSnapshotActionSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    snapshot_id = serializers.IntegerField(required=False, min_value=1)


class RuntimeTaskListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeConfigVersionListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeWorkspaceFileListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeAuditLogListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeWorkspaceTransferTaskListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeLighthouseCatalogSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    region = serializers.CharField(required=False, allow_blank=True)
    zone = serializers.CharField(required=False, allow_blank=True)
    blueprint_type = serializers.CharField(required=False, allow_blank=True)
    platform_type = serializers.CharField(required=False, allow_blank=True)
    keyword = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeLighthousePurchaseDryRunSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    region = serializers.CharField(required=False, allow_blank=True)
    zone = serializers.CharField(required=False, allow_blank=True)
    bundle_id = serializers.CharField(required=False, allow_blank=True)
    blueprint_id = serializers.CharField(required=False, allow_blank=True)
    period = serializers.IntegerField(required=False, min_value=1, max_value=60)
    renew_flag = serializers.CharField(required=False, allow_blank=True)
    firewall_template_id = serializers.CharField(required=False, allow_blank=True)
    key_id = serializers.CharField(required=False, allow_blank=True)
    client_token = serializers.CharField(required=False, allow_blank=True, max_length=64)


class RuntimeProvisionSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    region = serializers.CharField(required=False, allow_blank=True)
    zone = serializers.CharField(required=False, allow_blank=True)
    provider = serializers.ChoiceField(choices=["docker_shared_host", "tencent_lighthouse"], required=False)
    instance_id = serializers.CharField(required=False, allow_blank=True)
    instance_name = serializers.CharField(required=False, allow_blank=True)
    public_ip = serializers.CharField(required=False, allow_blank=True)
    private_ip = serializers.CharField(required=False, allow_blank=True)
    expired_at = serializers.CharField(required=False, allow_blank=True)
    bundle_id = serializers.CharField(required=False, allow_blank=True)
    blueprint_id = serializers.CharField(required=False, allow_blank=True)
    period = serializers.IntegerField(required=False, min_value=1, max_value=60)
    renew_flag = serializers.CharField(required=False, allow_blank=True)
    firewall_template_id = serializers.CharField(required=False, allow_blank=True)
    key_id = serializers.CharField(required=False, allow_blank=True)
    client_token = serializers.CharField(required=False, allow_blank=True, max_length=64)
    dry_run = serializers.BooleanField(required=False, default=False)


class RuntimeBootstrapSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeDeployBridgeSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    install_docker = serializers.BooleanField(default=False)
    auto_bootstrap = serializers.BooleanField(default=True)


class RuntimeHealthCheckSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeConfigPublishSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    notes = serializers.CharField(required=False, allow_blank=True)


class RuntimeConfigReadbackSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeConfigDiffSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeGatewayStateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeCronJobListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeCronRunListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    id = serializers.CharField(max_length=255)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)


class RuntimeCronJobCreateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    name = serializers.CharField(max_length=255)
    schedule_type = serializers.ChoiceField(choices=["at", "every", "cron"], default="every")
    schedule_value = serializers.CharField(max_length=255)
    message = serializers.CharField()
    agent_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    session = serializers.CharField(max_length=255, required=False, allow_blank=True)
    enabled = serializers.BooleanField(default=True)

    def validate_message(self, value):
        normalized = str(value or "").strip()
        if not normalized:
            raise serializers.ValidationError("message is required")
        return normalized

    def validate_schedule_value(self, value):
        normalized = str(value or "").strip()
        if not normalized:
            raise serializers.ValidationError("schedule_value is required")
        return normalized


class RuntimeCronJobActionSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    id = serializers.CharField(max_length=255)


class RuntimeAgentWorkspaceToolExecuteSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=255)
    tool = serializers.ChoiceField(
        choices=[
            "touming_workspace_list",
            "touming_workspace_locate",
            "touming_workspace_read",
            "touming_workspace_unzip",
            "touming_workspace_write",
            "touming_workspace_rename",
            "touming_workspace_delete",
            "workspace.list",
            "workspace.locate",
            "workspace.read",
            "workspace.unzip",
            "workspace.write",
            "workspace.rename",
            "workspace.delete",
        ]
    )
    arguments = serializers.JSONField(required=False)
    conversation_id = serializers.CharField(required=False, allow_blank=True)
    session_key = serializers.CharField(required=False, allow_blank=True)

    def validate_arguments(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("arguments must be an object")
        return value
    
    def validate(self, attrs): #跨字段规则校验
        tool = str(attrs.get("tool") or "").strip()
        arguments = attrs.get("arguments") or {}

        if tool not in {"touming_workspace_write", "workspace.write","touming_workspace_rename", "workspace.rename"}:
            return attrs
   
        if not isinstance(arguments, dict):
            raise serializers.ValidationError({"arguments": "arguments must be an object"})

        if tool in {"touming_workspace_write", "workspace.write"}:
            allowed_keys = {"logical_path", "path", "content", "overwrite", "create_dirs", "encoding"}
            unknown_keys = sorted(set(arguments.keys()) - allowed_keys)
            if unknown_keys:
                raise serializers.ValidationError(
                    {"arguments": f"unsupported write arguments: {', '.join(unknown_keys)}"}
                )

            raw_path = str(arguments.get("logical_path") or arguments.get("path") or "").strip()
            if not raw_path:
                raise serializers.ValidationError({"arguments": "logical_path is required for write tool"})
            try:
                normalized_path = validate_workspace_logical_path(raw_path, "logical_path")
            except ValueError as exc:
                raise serializers.ValidationError({"arguments": str(exc)}) from exc

            if "content" not in arguments:
                raise serializers.ValidationError({"arguments": "content is required for write tool"})
            content = arguments.get("content")
            if not isinstance(content, str):
                raise serializers.ValidationError({"arguments": "content must be a UTF-8 string"})
            if "\x00" in content:
                raise serializers.ValidationError({"arguments": "content contains invalid characters"})
            encoding = str(arguments.get("encoding") or "utf-8").strip().lower()
            if encoding not in {"utf-8", "utf8", "text"}:
                raise serializers.ValidationError({"arguments": "encoding is not supported"})

            try:
                content_bytes = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise serializers.ValidationError({"arguments": "content must be valid UTF-8"}) from exc

            max_write_bytes_raw = getattr(settings, "RUNTIME_WORKSPACE_WRITE_MAX_BYTES", 1024 * 1024)
            try:
                max_write_bytes = int(max_write_bytes_raw)
            except (TypeError, ValueError):
                max_write_bytes = 1024 * 1024
            max_write_bytes = max(1024, min(max_write_bytes, 10 * 1024 * 1024))

            if len(content_bytes) > max_write_bytes:
                raise serializers.ValidationError(
                    {"arguments": f"content is too large (max {max_write_bytes} bytes)"}
                )

            overwrite = arguments.get("overwrite", False)
            if not isinstance(overwrite, bool):
                raise serializers.ValidationError({"arguments": "overwrite must be a boolean"})

            create_dirs = arguments.get("create_dirs", False)
            if not isinstance(create_dirs, bool):
                raise serializers.ValidationError({"arguments": "create_dirs must be a boolean"})
            attrs["arguments"] = {
            "logical_path": normalized_path,
            "content": content,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "encoding": "utf-8",
            }
            return attrs
    
        allowed_keys = {"logical_path", "path", "file_name", "name", "new_logical_path", "new_path", "target_path"}
        unknown_keys = sorted(set(arguments.keys()) - allowed_keys)
        if unknown_keys:
            raise serializers.ValidationError(
                {"arguments": f"unsupported rename arguments: {', '.join(unknown_keys)}"}
            )
        source_logical_path = str(arguments.get("logical_path") or arguments.get("path") or "").strip()
        source_file_name = str(arguments.get("file_name") or arguments.get("name") or "").strip()
        if not source_logical_path and not source_file_name:
            raise serializers.ValidationError(
                {"arguments": "logical_path or file_name is required for rename tool"}
            )
        
        normalized_source_path = ""
        if source_logical_path:
            try:
                normalized_source_path = validate_workspace_logical_path(source_logical_path, "logical_path")
            except ValueError as exc:
                raise serializers.ValidationError({"arguments": str(exc)}) from exc

        target_logical_path = str(
            arguments.get("new_logical_path") or arguments.get("new_path") or arguments.get("target_path") or ""
        ).strip()
        if not target_logical_path:
            raise serializers.ValidationError({"arguments": "new_logical_path is required for rename tool"})
        try:
            normalized_target_path = validate_workspace_logical_path(target_logical_path, "new_logical_path")
        except ValueError as exc:
            raise serializers.ValidationError({"arguments": str(exc)}) from exc

        attrs["arguments"] = {
            "logical_path": normalized_source_path,
            "file_name": source_file_name,
            "new_logical_path": normalized_target_path,
        }
        return attrs



class RuntimeWorkspaceUploadTicketSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    logical_path = serializers.CharField(max_length=512)
    file_name = serializers.CharField(max_length=255)
    mime_type = serializers.CharField(max_length=128, required=False, allow_blank=True)
    size = serializers.IntegerField(required=False, min_value=0)

    def validate_logical_path(self, value):
        return _validate_workspace_logical_path(value)

    def validate_file_name(self, value):
        return _validate_workspace_file_name(value)


class RuntimeWorkspaceUploadFileSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    logical_path = serializers.CharField(max_length=512)
    file = serializers.FileField()
    file_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    mime_type = serializers.CharField(max_length=128, required=False, allow_blank=True)
    size = serializers.IntegerField(required=False, min_value=0)

    def validate_logical_path(self, value):
        return _validate_workspace_logical_path(value)

    def validate_file_name(self, value):
        return _validate_workspace_file_name(value)


class RuntimeWorkspaceDownloadTicketSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    file_id = serializers.IntegerField(min_value=1)


class RuntimeWorkspaceFileDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    file_id = serializers.IntegerField(min_value=1)


class RuntimeWorkspaceFileDeleteSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    file_id = serializers.IntegerField(min_value=1)


class RuntimeWorkspaceFileRenameSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    file_id = serializers.IntegerField(min_value=1)
    new_logical_path = serializers.CharField(max_length=512)

    def validate_new_logical_path(self, value):
        return _validate_workspace_logical_path(value)


class RuntimeWorkspacePathSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    logical_path = serializers.CharField(max_length=512)

    def validate_logical_path(self, value):
        return _validate_workspace_logical_path(value)


class RuntimeWorkspacePathRenameSerializer(RuntimeWorkspacePathSerializer):
    new_logical_path = serializers.CharField(max_length=512)

    def validate_new_logical_path(self, value):
        return _validate_workspace_logical_path(value)


class RuntimeRolloutPolicyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeRolloutPolicyUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    strategy = serializers.ChoiceField(choices=["direct", "canary"])
    status = serializers.ChoiceField(choices=["active", "paused"], required=False)
    canary_percentage = serializers.IntegerField(required=False, min_value=0, max_value=100)
    auto_promote = serializers.BooleanField(required=False)
    auto_promote_minutes = serializers.IntegerField(required=False, min_value=1, max_value=1440)
    rollback_on_alert = serializers.BooleanField(required=False)


class RuntimeAlertPolicyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class RuntimeAlertPolicyUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(choices=["active", "muted"], required=False)
    channels = serializers.ListField(child=serializers.CharField(max_length=32), required=False)
    config_drift_enabled = serializers.BooleanField(required=False)
    gateway_unhealthy_enabled = serializers.BooleanField(required=False)
    workspace_sync_failed_enabled = serializers.BooleanField(required=False)
    delivery_target = serializers.CharField(required=False, allow_blank=True)


class RuntimeDriftEventListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    status = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class RuntimeDriftEventResolveSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    event_id = serializers.IntegerField(min_value=1)
    resolution = serializers.ChoiceField(choices=["republish", "accept_runtime", "manual"])

