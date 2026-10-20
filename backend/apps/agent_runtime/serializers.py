"""Request validation for Agent Runtime operational APIs."""

from rest_framework import serializers

from apps.agent_runtime.models import AgentRun


class CompanyRuntimeQuerySerializer(serializers.Serializer):
    """Validate the server-authorized company selector shared by ops APIs."""

    cid = serializers.CharField(max_length=255)


class PageQuerySerializer(CompanyRuntimeQuerySerializer):
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class RunListQuerySerializer(PageQuerySerializer):
    status = serializers.ChoiceField(
        required=False,
        choices=("PENDING", "RUNNING", "WAITING_TOOL", "CANCELLING", "COMPLETED", "FAILED", "CANCELLED"),
    )
    agent_id = serializers.IntegerField(required=False, min_value=1)


class MemoryJobListQuerySerializer(PageQuerySerializer):
    status = serializers.ChoiceField(
        required=False,
        choices=("pending", "running", "completed", "failed"),
    )
    job_type = serializers.ChoiceField(
        required=False,
        choices=("knowledge_index", "memory_commit", "delete"),
    )
    agent_id = serializers.IntegerField(required=False, min_value=1)


class KnowledgeFileListQuerySerializer(PageQuerySerializer):
    status = serializers.ChoiceField(
        required=False,
        choices=("pending", "indexing", "completed", "failed"),
    )


class CompanyQuerySerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class KnowledgeTreeQuerySerializer(CompanyQuerySerializer):
    logical_path = serializers.CharField(max_length=512, required=False, default="knowledge")
    recursive = serializers.BooleanField(required=False, default=True)


class KnowledgeFolderCreateSerializer(CompanyQuerySerializer):
    logical_path = serializers.CharField(max_length=512)


class KnowledgePathDeleteSerializer(CompanyQuerySerializer):
    logical_path = serializers.CharField(max_length=512)


class KnowledgeFileContentQuerySerializer(CompanyQuerySerializer):
    logical_path = serializers.CharField(max_length=512)


class KnowledgeUploadSerializer(CompanyQuerySerializer):
    logical_path = serializers.CharField(max_length=512)
    file = serializers.FileField()


class ScheduleListQuerySerializer(PageQuerySerializer):
    status = serializers.ChoiceField(
        required=False,
        choices=("active", "paused", "failed"),
    )
    agent_id = serializers.IntegerField(required=False, min_value=1)


class ScheduleCreateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    name = serializers.CharField(max_length=255, label="任务名称")
    schedule_type = serializers.ChoiceField(
        choices=("at", "every", "cron"),
        label="任务类型",
    )
    schedule_value = serializers.CharField(max_length=255, label="执行时间")
    message = serializers.CharField(label="任务描述")
    agent_id = serializers.CharField(max_length=255, label="执行智能体")
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


class RuntimeAlertsQuerySerializer(CompanyRuntimeQuerySerializer):
    pass


class RuntimeMetricsQuerySerializer(CompanyRuntimeQuerySerializer):
    window_minutes = serializers.IntegerField(required=False, default=1440, min_value=1, max_value=10080)


class AgentRunListQuerySerializer(CompanyRuntimeQuerySerializer):
    status = serializers.ChoiceField(choices=AgentRun.STATUS_CHOICES, required=False)
    source = serializers.ChoiceField(choices=AgentRun.SOURCE_CHOICES, required=False)
    agent_id = serializers.CharField(max_length=255, required=False)
    user_id = serializers.CharField(max_length=255, required=False)
    session_id = serializers.CharField(max_length=255, required=False)
    trace_id = serializers.CharField(max_length=255, required=False)
    created_from = serializers.DateTimeField(required=False)
    created_to = serializers.DateTimeField(required=False)
    cursor = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=25)
    offset = serializers.IntegerField(min_value=0, default=0)

    def validate(self, attrs):
        created_from = attrs.get("created_from")
        created_to = attrs.get("created_to")
        if created_from and created_to and created_from > created_to:
            raise serializers.ValidationError("created_from 不能晚于 created_to")
        return attrs


class AgentRunDetailQuerySerializer(CompanyRuntimeQuerySerializer):
    pass
