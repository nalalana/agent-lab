"""Persistent records for unified Agent execution and configuration snapshots."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models


class CompanyAgentRuntimePolicy(models.Model):
    """Controls how one company is routed to the unified runtime."""

    MODE_LEGACY = 'legacy'
    MODE_CANARY = 'canary'
    MODE_UNIFIED = 'unified'
    MODE_CHOICES = [(value, value) for value in (MODE_LEGACY, MODE_CANARY, MODE_UNIFIED)]
    NETWORK_ISOLATED = 'isolated'

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(
        'company.Company',
        on_delete=models.CASCADE,
        related_name='agent_runtime_policy',
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, default=MODE_LEGACY)
    canary_percentage = models.PositiveSmallIntegerField(default=0)
    sandbox_template = models.CharField(max_length=128, blank=True, default='')
    network_policy = models.CharField(max_length=16, default=NETWORK_ISOLATED)
    max_concurrent_runs = models.PositiveIntegerField(default=5)
    max_sandbox_seconds = models.PositiveIntegerField(default=300)
    daily_token_budget = models.PositiveBigIntegerField(null=True, blank=True)
    daily_sandbox_seconds_budget = models.PositiveBigIntegerField(null=True, blank=True)
    config_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'agent_runtime_company_runtime_policy'
        constraints = [
            models.CheckConstraint(
                check=models.Q(canary_percentage__gte=0, canary_percentage__lte=100),
                name='agent_runtime_canary_pct_range',
            ),
        ]


class KnowledgeFolder(models.Model):
    """Persistent empty directories for the native knowledge workspace."""

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.CASCADE,
        related_name='knowledge_folders',
    )
    logical_path = models.CharField(max_length=512)
    created_by = models.CharField(max_length=128, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'agent_runtime_knowledge_folder'
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'logical_path'],
                name='agent_runtime_knowledge_folder_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['company', 'logical_path'],
                name='agent_runtime_kfolder_path_idx',
            ),
        ]


class AgentConfigSnapshot(models.Model):
    """Immutable, secret-free configuration used by one or more Agent runs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='agent_config_snapshots',
    )
    agent = models.ForeignKey(
        'company.TransparentCompanyAgentDefinition',
        on_delete=models.PROTECT,
        related_name='config_snapshots',
    )
    company_prompt_version = models.ForeignKey(
        'company.TransparentCompanyPromptVersion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='config_snapshots',
    )
    agent_prompt_version = models.ForeignKey(
        'company.TransparentCompanyAgentPromptVersion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='config_snapshots',
    )
    skill_versions = models.JSONField(default=dict, blank=True)
    model_config = models.JSONField(default=dict, blank=True)
    tool_policy = models.JSONField(default=dict, blank=True)
    memory_policy = models.JSONField(default=dict, blank=True)
    compiled_prompt_encrypted = models.TextField(blank=True, default='')
    checksum = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_runtime_agent_config_snapshot'

    def save(self, *args, **kwargs):
        """Reject updates so a historical run can always reproduce its config."""
        if not self._state.adding:
            raise ValidationError('AgentConfigSnapshot is immutable; create a new snapshot.')
        return super().save(*args, **kwargs)


class AgentRun(models.Model):
    """One idempotent Agent request and its execution lifecycle."""

    SOURCE_AGENT_TEST = 'agent_test'
    SOURCE_PUBLIC_CLIENT = 'public_client'
    SOURCE_CRON = 'cron'
    SOURCE_CHOICES = [
        (value, value)
        for value in (SOURCE_AGENT_TEST, SOURCE_PUBLIC_CLIENT, SOURCE_CRON)
    ]
    # Compatibility names for in-flight Phase 1 branches. Their persisted
    # values still follow the frozen Phase 0 RunRequest contract.
    SOURCE_TEST = SOURCE_AGENT_TEST
    SOURCE_PUBLIC = SOURCE_PUBLIC_CLIENT
    ENGINE_OPENCLAW = 'openclaw'
    ENGINE_UNIFIED = 'unified'
    ENGINE_CHOICES = [(value, value) for value in (ENGINE_OPENCLAW, ENGINE_UNIFIED)]

    STATUS_PENDING = 'PENDING'
    STATUS_RUNNING = 'RUNNING'
    STATUS_WAITING_TOOL = 'WAITING_TOOL'
    STATUS_CANCELLING = 'CANCELLING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (value, value)
        for value in (
            STATUS_PENDING,
            STATUS_RUNNING,
            STATUS_WAITING_TOOL,
            STATUS_CANCELLING,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_CANCELLED,
        )
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.CharField(max_length=128)
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT, related_name='agent_runs'
    )
    agent = models.ForeignKey(
        'company.TransparentCompanyAgentDefinition',
        on_delete=models.PROTECT,
        related_name='runs',
    )
    user = models.ForeignKey(
        'user.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_runs'
    )
    session = models.ForeignKey(
        'company.TransparentCompanyAgentTestSession',
        on_delete=models.PROTECT,
        related_name='agent_runs',
    )
    source = models.CharField(max_length=24, choices=SOURCE_CHOICES)
    engine = models.CharField(max_length=16, choices=ENGINE_CHOICES)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    config_snapshot = models.ForeignKey(
        AgentConfigSnapshot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='runs',
    )
    input_message = models.ForeignKey(
        'company.TransparentCompanyAgentTestMessage',
        on_delete=models.PROTECT,
        related_name='input_for_runs',
    )
    output_message = models.ForeignKey(
        'company.TransparentCompanyAgentTestMessage',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='output_for_runs',
    )
    model_provider = models.CharField(max_length=64, blank=True, default='')
    model_name = models.CharField(max_length=128, blank=True, default='')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    sandbox_seconds = models.PositiveIntegerField(default=0)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    first_token_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_runtime_agent_run'
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'request_id'], name='agent_run_company_request_uniq'
            ),
        ]
        indexes = [
            models.Index(fields=['company', '-created_at'], name='agent_run_company_created_idx'),
            models.Index(fields=['company', 'status'], name='agent_run_company_status_idx'),
            models.Index(fields=['session', 'created_at'], name='agent_run_session_created_idx'),
        ]


class AgentRunEvent(models.Model):
    """Sparse durable events; streaming token deltas are deliberately excluded."""

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='events')
    sequence = models.PositiveIntegerField()
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_runtime_agent_run_event'
        ordering = ['sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'sequence'], name='agent_run_event_sequence_uniq'
            ),
        ]


class AgentToolCall(models.Model):
    """Durable audit record for one model-requested tool call."""

    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_RUNNING, STATUS_RUNNING),
        (STATUS_COMPLETED, STATUS_COMPLETED),
        (STATUS_FAILED, STATUS_FAILED),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='tool_calls')
    model_call_id = models.CharField(max_length=255)
    tool_name = models.CharField(max_length=128)
    executor = models.CharField(max_length=32)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    arguments_redacted = models.JSONField(default=dict, blank=True)
    arguments_hash = models.CharField(max_length=64)
    result_ref = models.CharField(max_length=1024, blank=True, default='')
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_tool_call'
        constraints = [
            models.UniqueConstraint(fields=['run', 'model_call_id'], name='agent_tool_call_run_model_uniq'),
        ]
        indexes = [
            models.Index(fields=['run', 'status'], name='agent_tool_call_run_status_idx'),
            models.Index(fields=['tool_name', 'created_at'], name='tool_call_tool_created_idx'),
        ]


class SandboxExecution(models.Model):
    """Auditable lifecycle state for the single sandbox owned by one Run."""

    PROVIDER_TENCENT_AGSX = 'tencent_agsx'
    PROVIDER_CHOICES = [(PROVIDER_TENCENT_AGSX, PROVIDER_TENCENT_AGSX)]

    STATUS_CREATING = 'creating'
    STATUS_READY = 'ready'
    STATUS_RUNNING = 'running'
    STATUS_TERMINATED = 'terminated'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (value, value)
        for value in (
            STATUS_CREATING,
            STATUS_READY,
            STATUS_RUNNING,
            STATUS_TERMINATED,
            STATUS_FAILED,
        )
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(
        AgentRun,
        on_delete=models.CASCADE,
        related_name='sandbox_execution',
    )
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='sandbox_executions',
    )
    provider = models.CharField(
        max_length=32,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_TENCENT_AGSX,
    )
    external_id = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATING)
    template = models.CharField(max_length=128)
    cpu_cores = models.PositiveIntegerField()
    memory_mib = models.PositiveIntegerField()
    network_policy = models.CharField(max_length=16)
    expires_at = models.DateTimeField()
    ready_at = models.DateTimeField(null=True, blank=True)
    terminated_at = models.DateTimeField(null=True, blank=True)
    billed_seconds = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sandbox_execution'
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='sandbox_status_expiry_idx'),
            models.Index(fields=['company', 'created_at'], name='sandbox_company_created_idx'),
        ]


class MemorySyncJob(models.Model):
    """Durable queue state for asynchronous OpenViking mutations."""

    TYPE_KNOWLEDGE_INDEX = 'knowledge_index'
    TYPE_MEMORY_COMMIT = 'memory_commit'
    TYPE_DELETE = 'delete'
    TYPE_CHOICES = [
        (TYPE_KNOWLEDGE_INDEX, TYPE_KNOWLEDGE_INDEX),
        (TYPE_MEMORY_COMMIT, TYPE_MEMORY_COMMIT),
        (TYPE_DELETE, TYPE_DELETE),
    ]

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, STATUS_PENDING),
        (STATUS_RUNNING, STATUS_RUNNING),
        (STATUS_COMPLETED, STATUS_COMPLETED),
        (STATUS_FAILED, STATUS_FAILED),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='memory_sync_jobs',
    )
    agent = models.ForeignKey(
        'company.TransparentCompanyAgentDefinition',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='memory_sync_jobs',
    )
    user = models.ForeignKey(
        'user.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='memory_sync_jobs',
    )
    job_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    idempotency_key = models.CharField(max_length=255, unique=True)
    source_ref = models.CharField(max_length=1024)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    external_session_id = models.CharField(max_length=255, blank=True, default='')
    external_task_id = models.CharField(max_length=255, blank=True, default='')
    external_archive_uri = models.CharField(max_length=1024, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'agent_runtime_memory_sync_job'
        indexes = [
            models.Index(fields=['status', 'next_retry_at'], name='memory_job_status_retry_idx'),
            models.Index(fields=['company', 'created_at'], name='memory_job_company_created_idx'),
        ]

class AgentSchedule(models.Model):
    """Platform-owned scheduled Agent trigger executed through AgentRun."""

    SCHEDULE_AT = 'at'
    SCHEDULE_EVERY = 'every'
    SCHEDULE_CRON = 'cron'
    SCHEDULE_TYPE_CHOICES = [
        (SCHEDULE_AT, SCHEDULE_AT),
        (SCHEDULE_EVERY, SCHEDULE_EVERY),
        (SCHEDULE_CRON, SCHEDULE_CRON),
    ]
    STATUS_ACTIVE = 'active'
    STATUS_PAUSED = 'paused'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, STATUS_ACTIVE),
        (STATUS_PAUSED, STATUS_PAUSED),
        (STATUS_FAILED, STATUS_FAILED),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='agent_schedules',
    )
    agent = models.ForeignKey(
        'company.TransparentCompanyAgentDefinition',
        on_delete=models.PROTECT,
        related_name='runtime_schedules',
    )
    operator = models.ForeignKey(
        'user.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_schedules',
    )
    name = models.CharField(max_length=255)
    schedule_type = models.CharField(max_length=16, choices=SCHEDULE_TYPE_CHOICES)
    schedule_value = models.CharField(max_length=255)
    message = models.TextField()
    session_key = models.CharField(max_length=255, blank=True, default='')
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run = models.ForeignKey(
        AgentRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='last_for_schedules',
    )
    last_error = models.TextField(blank=True, default='')
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    retry_after = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'agent_runtime_agent_schedule'
        indexes = [
            models.Index(fields=['enabled', 'status', 'next_run_at'], name='agent_sched_due_idx'),
            models.Index(fields=['company', 'created_at'], name='agent_sched_company_idx'),
        ]
