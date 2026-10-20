"""
Runtime domain models for OpenClaw instance orchestration.
"""
from django.db import models

from apps.company.models import Company

class RuntimeServerResource(models.Model):
    PROVIDER_DOCKER_SHARED_HOST = "docker_shared_host"
    PROVIDER_TENCENT_LIGHTHOUSE = "tencent_lighthouse"
    PROVIDER_CHOICES = [
        (PROVIDER_DOCKER_SHARED_HOST, "Shared host Docker"),
        (PROVIDER_TENCENT_LIGHTHOUSE, "Tencent Lighthouse"),
    ]

    CLOUD_VENDOR_UNICOM = "china_unicom_cloud"
    CLOUD_VENDOR_TENCENT = "tencent_cloud"
    CLOUD_VENDOR_MANUAL = "manual"
    CLOUD_VENDOR_CHOICES = [
        (CLOUD_VENDOR_UNICOM, "China Unicom Cloud"),
        (CLOUD_VENDOR_TENCENT, "Tencent Cloud"),
        (CLOUD_VENDOR_MANUAL, "Manual"),
    ]

    STATUS_AVAILABLE = "available"
    STATUS_BINDING = "binding"
    STATUS_BOUND = "bound"
    STATUS_PROVISIONING = "provisioning"
    STATUS_RUNNING = "running"
    STATUS_UNHEALTHY = "unhealthy"
    STATUS_EXPIRING = "expiring"
    STATUS_EXPIRED = "expired"
    STATUS_RELEASED = "released"
    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_BINDING, "Binding"),
        (STATUS_BOUND, "Bound"),
        (STATUS_PROVISIONING, "Provisioning"),
        (STATUS_RUNNING, "Running"),
        (STATUS_UNHEALTHY, "Unhealthy"),
        (STATUS_EXPIRING, "Expiring"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_RELEASED, "Released"),
    ]

    RENEWAL_MANUAL = "manual"
    RENEWAL_SKIPPED = "skipped"
    RENEWAL_RENEWED = "renewed"
    RENEWAL_UNKNOWN = "unknown"
    RENEWAL_STATUS_CHOICES = [
        (RENEWAL_MANUAL, "Manual"),
        (RENEWAL_SKIPPED, "Skipped"),
        (RENEWAL_RENEWED, "Renewed"),
        (RENEWAL_UNKNOWN, "Unknown"),
    ]

    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=64, choices=PROVIDER_CHOICES)
    cloud_vendor = models.CharField(max_length=64, choices=CLOUD_VENDOR_CHOICES, default=CLOUD_VENDOR_MANUAL)
    instance_id = models.CharField(max_length=128, null=True, blank=True)
    instance_name = models.CharField(max_length=255, null=True, blank=True)
    host_id = models.CharField(max_length=128, null=True, blank=True)
    public_ip = models.CharField(max_length=64, null=True, blank=True)
    private_ip = models.CharField(max_length=64, null=True, blank=True)
    api_base = models.CharField(max_length=255, null=True, blank=True)
    region = models.CharField(max_length=64, null=True, blank=True)
    zone = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_AVAILABLE)
    resource_spec = models.TextField(null=True, blank=True)
    expired_at = models.CharField(max_length=20, null=True, blank=True)
    renewal_status = models.CharField(max_length=32, choices=RENEWAL_STATUS_CHOICES, default=RENEWAL_UNKNOWN)
    security_group_id = models.CharField(max_length=128, null=True, blank=True)
    image_id = models.CharField(max_length=128, null=True, blank=True)
    last_health_check_at = models.CharField(max_length=20, null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_serverresource"
        indexes = [
            models.Index(fields=["provider", "status"]),
            models.Index(fields=["cloud_vendor", "instance_id"]),
            models.Index(fields=["public_ip"]),
            models.Index(fields=["expired_at"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "provider": self.provider,
            "cloud_vendor": self.cloud_vendor,
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "host_id": self.host_id,
            "public_ip": self.public_ip,
            "private_ip": self.private_ip,
            "api_base": self.api_base,
            "region": self.region,
            "zone": self.zone,
            "status": self.status,
            "resource_spec": self.resource_spec,
            "expired_at": self.expired_at,
            "renewal_status": self.renewal_status,
            "security_group_id": self.security_group_id,
            "image_id": self.image_id,
            "last_health_check_at": self.last_health_check_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

class OpenClawInstance(models.Model):
    PROVIDER_DOCKER_SHARED_HOST = "docker_shared_host"
    PROVIDER_TENCENT_LIGHTHOUSE = "tencent_lighthouse"
    PROVIDER_CHOICES = [
        (PROVIDER_DOCKER_SHARED_HOST, "共享宿主机 Docker"),
        (PROVIDER_TENCENT_LIGHTHOUSE, "腾讯云 Lighthouse"),
    ]

    STATUS_PENDING = "pending"
    STATUS_PROVISIONING = "provisioning"
    STATUS_PROVISION_FAILED = "provision_failed"
    STATUS_BOOTSTRAPPING = "bootstrapping"
    STATUS_BOOTSTRAP_FAILED = "bootstrap_failed"
    STATUS_RUNNING = "running"
    STATUS_DEGRADED = "degraded"
    STATUS_SUSPENDED = "suspended"
    STATUS_DESTROYED = "destroyed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待创建"),
        (STATUS_PROVISIONING, "创建中"),
        (STATUS_PROVISION_FAILED, "创建失败"),
        (STATUS_BOOTSTRAPPING, "部署中"),
        (STATUS_BOOTSTRAP_FAILED, "部署失败"),
        (STATUS_RUNNING, "运行中"),
        (STATUS_DEGRADED, "降级运行"),
        (STATUS_SUSPENDED, "已暂停"),
        (STATUS_DESTROYED, "已销毁"),
    ]

    OPENCLAW_STATUS_UNKNOWN = "unknown"
    OPENCLAW_STATUS_STARTING = "starting"
    OPENCLAW_STATUS_HEALTHY = "healthy"
    OPENCLAW_STATUS_UNHEALTHY = "unhealthy"
    OPENCLAW_STATUS_STOPPED = "stopped"
    OPENCLAW_STATUS_CHOICES = [
        (OPENCLAW_STATUS_UNKNOWN, "未知"),
        (OPENCLAW_STATUS_STARTING, "启动中"),
        (OPENCLAW_STATUS_HEALTHY, "健康"),
        (OPENCLAW_STATUS_UNHEALTHY, "异常"),
        (OPENCLAW_STATUS_STOPPED, "已停止"),
    ]

    CONFIG_STATUS_PENDING = "pending"
    CONFIG_STATUS_IN_SYNC = "in_sync"
    CONFIG_STATUS_DRIFTED = "drifted"
    CONFIG_STATUS_READBACK_FAILED = "readback_failed"
    CONFIG_STATUS_CHOICES = [
        (CONFIG_STATUS_PENDING, "待同步"),
        (CONFIG_STATUS_IN_SYNC, "已同步"),
        (CONFIG_STATUS_DRIFTED, "已漂移"),
        (CONFIG_STATUS_READBACK_FAILED, "回读失败"),
    ]

    CREATE_SOURCE_PLATFORM = "platform"
    CREATE_SOURCE_MANUAL = "manual"
    CREATE_SOURCE_CHOICES = [
        (CREATE_SOURCE_PLATFORM, "平台创建"),
        (CREATE_SOURCE_MANUAL, "人工创建"),
    ]

    WORKSPACE_MODE_COS_BACKED = "cos_backed"
    WORKSPACE_MODE_LOCAL_ONLY = "local_only"
    WORKSPACE_MODE_CHOICES = [
        (WORKSPACE_MODE_COS_BACKED, "COS 驱动"),
        (WORKSPACE_MODE_LOCAL_ONLY, "本地存储"),
    ]

    CONNECTION_MODE_PLATFORM_SSH = "platform_ssh"
    CONNECTION_MODE_AGENT = "agent"
    CONNECTION_MODE_CHOICES = [
        (CONNECTION_MODE_PLATFORM_SSH, "平台 SSH"),
        (CONNECTION_MODE_AGENT, "实例 Agent"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_instances")
    provider = models.CharField(max_length=64, choices=PROVIDER_CHOICES, default=PROVIDER_DOCKER_SHARED_HOST)
    region = models.CharField(max_length=64, null=True, blank=True)
    zone = models.CharField(max_length=64, null=True, blank=True)
    instance_id = models.CharField(max_length=128, null=True, blank=True)
    instance_name = models.CharField(max_length=255, null=True, blank=True)
    host_id = models.CharField(max_length=128, null=True, blank=True)
    runtime_namespace = models.CharField(max_length=255, null=True, blank=True)
    gateway_endpoint = models.CharField(max_length=255, null=True, blank=True)
    gateway_port = models.IntegerField(default=18789)
    config_path = models.CharField(max_length=255, null=True, blank=True)
    workspace_prefix = models.CharField(max_length=255, null=True, blank=True)
    workspace_mode = models.CharField(max_length=32, choices=WORKSPACE_MODE_CHOICES, default=WORKSPACE_MODE_COS_BACKED)
    connection_mode = models.CharField(max_length=32, choices=CONNECTION_MODE_CHOICES, default=CONNECTION_MODE_PLATFORM_SSH)
    public_ip = models.CharField(max_length=64, null=True, blank=True)
    private_ip = models.CharField(max_length=64, null=True, blank=True)
    server_resource = models.ForeignKey(to=RuntimeServerResource, on_delete=models.SET_NULL, null=True, blank=True, related_name="runtime_instances")
    billing_order_id = models.CharField(max_length=128, null=True, blank=True)
    billing_plan_id = models.CharField(max_length=128, null=True, blank=True)
    expired_at = models.CharField(max_length=20, null=True, blank=True)
    renewal_status = models.CharField(max_length=32, null=True, blank=True)
    latest_snapshot_id = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    openclaw_status = models.CharField(max_length=32, choices=OPENCLAW_STATUS_CHOICES, default=OPENCLAW_STATUS_UNKNOWN)
    config_status = models.CharField(max_length=32, choices=CONFIG_STATUS_CHOICES, default=CONFIG_STATUS_PENDING)
    deploy_version = models.CharField(max_length=64, null=True, blank=True)
    current_publish_version = models.CharField(max_length=64, null=True, blank=True)
    current_runtime_version = models.CharField(max_length=64, null=True, blank=True)
    create_source = models.CharField(max_length=32, choices=CREATE_SOURCE_CHOICES, default=CREATE_SOURCE_PLATFORM)
    last_health_check_at = models.CharField(max_length=32, null=True, blank=True)
    last_readback_at = models.CharField(max_length=32, null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_openclawinstance"
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["provider", "host_id"]),
            models.Index(fields=["runtime_namespace"]),
            models.Index(fields=["instance_id"]),
            models.Index(fields=["server_resource"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "provider": self.provider,
            "region": self.region,
            "zone": self.zone,
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "host_id": self.host_id,
            "runtime_namespace": self.runtime_namespace,
            "gateway_endpoint": self.gateway_endpoint,
            "gateway_port": self.gateway_port,
            "config_path": self.config_path,
            "workspace_prefix": self.workspace_prefix,
            "workspace_mode": self.workspace_mode,
            "connection_mode": self.connection_mode,
            "public_ip": self.public_ip,
            "private_ip": self.private_ip,
            "server_resource_id": self.server_resource_id,
            "billing_order_id": self.billing_order_id,
            "billing_plan_id": self.billing_plan_id,
            "expired_at": self.expired_at,
            "renewal_status": self.renewal_status,
            "latest_snapshot_id": self.latest_snapshot_id,
            "status": self.status,
            "openclaw_status": self.openclaw_status,
            "config_status": self.config_status,
            "deploy_version": self.deploy_version,
            "current_publish_version": self.current_publish_version,
            "current_runtime_version": self.current_runtime_version,
            "create_source": self.create_source,
            "last_health_check_at": self.last_health_check_at,
            "last_readback_at": self.last_readback_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OpenClawProvisionTask(models.Model):
    TASK_CREATE_INSTANCE = "create_instance"
    TASK_BOOTSTRAP_OPENCLAW = "bootstrap_openclaw"
    TASK_HEALTH_CHECK = "health_check"
    TASK_RETRY = "retry"
    TASK_CONFIG_READBACK = "config_readback"
    TASK_WORKSPACE_SYNC = "workspace_sync"
    TASK_SYNC_SERVER_RESOURCE = "sync_server_resource"
    TASK_DEPLOY_BRIDGE = "deploy_bridge"
    TASK_BACKUP_RUNTIME_STATE = "backup_runtime_state"
    TASK_RESTORE_RUNTIME_STATE = "restore_runtime_state"
    TASK_RELEASE_EXPIRED = "release_expired"
    TASK_TYPE_CHOICES = [
        (TASK_CREATE_INSTANCE, "创建实例"),
        (TASK_BOOTSTRAP_OPENCLAW, "部署 OpenClaw"),
        (TASK_HEALTH_CHECK, "健康检查"),
        (TASK_RETRY, "重试任务"),
        (TASK_CONFIG_READBACK, "配置回读"),
        (TASK_WORKSPACE_SYNC, "工作区同步"),
        (TASK_SYNC_SERVER_RESOURCE, "同步服务器资源"),
        (TASK_DEPLOY_BRIDGE, "部署运行时网桥"),
        (TASK_BACKUP_RUNTIME_STATE, "备份运行时状态"),
        (TASK_RESTORE_RUNTIME_STATE, "恢复运行时状态"),
        (TASK_RELEASE_EXPIRED, "释放到期资源"),
    ]

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待执行"),
        (STATUS_RUNNING, "执行中"),
        (STATUS_SUCCEEDED, "成功"),
        (STATUS_FAILED, "失败"),
        (STATUS_CANCELLED, "已取消"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_provision_tasks")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.CASCADE, related_name="tasks", null=True, blank=True)
    task_type = models.CharField(max_length=64, choices=TASK_TYPE_CHOICES)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    step = models.CharField(max_length=128, null=True, blank=True)
    retry_count = models.IntegerField(default=0)
    request_payload = models.TextField(null=True, blank=True)
    result_payload = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    started_at = models.CharField(max_length=20, null=True, blank=True)
    finished_at = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_openclawprovisiontask"
        indexes = [
            models.Index(fields=["company", "task_type", "status"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "task_type": self.task_type,
            "status": self.status,
            "step": self.step,
            "retry_count": self.retry_count,
            "request_payload": self.request_payload,
            "result_payload": self.result_payload,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OpenClawAuditLog(models.Model):
    ACTOR_SYSTEM = "system"
    ACTOR_USER = "user"
    ACTOR_ADMIN = "admin"
    ACTOR_TYPE_CHOICES = [
        (ACTOR_SYSTEM, "系统"),
        (ACTOR_USER, "用户"),
        (ACTOR_ADMIN, "管理员"),
    ]

    TARGET_INSTANCE = "instance"
    TARGET_TASK = "task"
    TARGET_CONFIG = "config"
    TARGET_AGENT = "agent"
    TARGET_SKILL = "skill"
    TARGET_FILE = "file"
    TARGET_CRON = "cron"
    TARGET_SERVER_RESOURCE = "server_resource"
    TARGET_BINDING = "binding"
    TARGET_SNAPSHOT = "snapshot"
    TARGET_TYPE_CHOICES = [
        (TARGET_INSTANCE, "实例"),
        (TARGET_TASK, "任务"),
        (TARGET_CONFIG, "配置"),
        (TARGET_AGENT, "Agent"),
        (TARGET_SKILL, "Skill"),
        (TARGET_FILE, "文件"),
        (TARGET_CRON, "Cron"),
        (TARGET_SERVER_RESOURCE, "Server resource"),
        (TARGET_BINDING, "Runtime binding"),
        (TARGET_SNAPSHOT, "Runtime snapshot"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_audit_logs")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    actor_type = models.CharField(max_length=32, choices=ACTOR_TYPE_CHOICES, default=ACTOR_SYSTEM)
    actor_id = models.CharField(max_length=128, null=True, blank=True)
    action = models.CharField(max_length=128)
    target_type = models.CharField(max_length=32, choices=TARGET_TYPE_CHOICES)
    target_id = models.CharField(max_length=128, null=True, blank=True)
    detail = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_openclawauditlog"
        indexes = [
            models.Index(fields=["company", "action"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
            "created_at": self.created_at,
        }

class CompanyRuntimeBinding(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_EXPIRED = "expired"
    STATUS_RELEASED = "released"
    STATUS_MIGRATED = "migrated"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_RELEASED, "Released"),
        (STATUS_MIGRATED, "Migrated"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_server_bindings")
    openclaw_instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.CASCADE, related_name="server_bindings")
    server_resource = models.ForeignKey(to=RuntimeServerResource, on_delete=models.CASCADE, related_name="company_bindings")
    billing_order_id = models.CharField(max_length=128, null=True, blank=True)
    billing_plan_id = models.CharField(max_length=128, null=True, blank=True)
    binding_status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    bound_at = models.CharField(max_length=20, null=True, blank=True)
    expired_at = models.CharField(max_length=20, null=True, blank=True)
    released_at = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_companyruntimebinding"
        indexes = [
            models.Index(fields=["company", "binding_status"]),
            models.Index(fields=["server_resource", "binding_status"]),
            models.Index(fields=["openclaw_instance"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "openclaw_instance_id": self.openclaw_instance_id,
            "server_resource_id": self.server_resource_id,
            "billing_order_id": self.billing_order_id,
            "billing_plan_id": self.billing_plan_id,
            "binding_status": self.binding_status,
            "bound_at": self.bound_at,
            "expired_at": self.expired_at,
            "released_at": self.released_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OpenClawRuntimeSnapshot(models.Model):
    TYPE_FULL = "full"
    TYPE_CONFIG = "config"
    TYPE_METADATA = "metadata"
    TYPE_CHOICES = [
        (TYPE_FULL, "Full"),
        (TYPE_CONFIG, "Config"),
        (TYPE_METADATA, "Metadata"),
    ]

    STATUS_PENDING = "pending"
    STATUS_UPLOADED = "uploaded"
    STATUS_FAILED = "failed"
    STATUS_RESTORED = "restored"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RESTORED, "Restored"),
        (STATUS_EXPIRED, "Expired"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_snapshots")
    openclaw_instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="runtime_snapshots")
    server_resource = models.ForeignKey(to=RuntimeServerResource, on_delete=models.SET_NULL, null=True, blank=True, related_name="runtime_snapshots")
    snapshot_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default=TYPE_FULL)
    snapshot_version = models.CharField(max_length=64, default="v1")
    cos_bucket = models.CharField(max_length=255, null=True, blank=True)
    cos_key = models.CharField(max_length=1024, null=True, blank=True)
    checksum = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    retention_until = models.CharField(max_length=20, null=True, blank=True)
    source_instance_id = models.CharField(max_length=128, null=True, blank=True)
    restored_to_instance_id = models.CharField(max_length=128, null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_openclawruntimesnapshot"
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["openclaw_instance"]),
            models.Index(fields=["server_resource"]),
            models.Index(fields=["retention_until"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "openclaw_instance_id": self.openclaw_instance_id,
            "server_resource_id": self.server_resource_id,
            "snapshot_type": self.snapshot_type,
            "snapshot_version": self.snapshot_version,
            "cos_bucket": self.cos_bucket,
            "cos_key": self.cos_key,
            "checksum": self.checksum,
            "status": self.status,
            "retention_until": self.retention_until,
            "source_instance_id": self.source_instance_id,
            "restored_to_instance_id": self.restored_to_instance_id,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

class OpenClawConfigVersion(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "草稿"),
        (STATUS_PUBLISHED, "已发布"),
        (STATUS_SUPERSEDED, "已过期"),
    ]

    SOURCE_PLATFORM_GUI = "platform_gui"
    SOURCE_GITEE_IMPORT = "gitee_import"
    SOURCE_CHOICES = [
        (SOURCE_PLATFORM_GUI, "平台 GUI"),
        (SOURCE_GITEE_IMPORT, "Gitee 导入"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_config_versions")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="config_versions")
    version = models.CharField(max_length=64, unique=True)
    schema_version = models.CharField(max_length=64, default="v2-preview")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_PLATFORM_GUI)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    created_by = models.CharField(max_length=128, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    config_payload = models.TextField()
    published_at = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_openclawconfigversion"
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "source": self.source,
            "status": self.status,
            "created_by": self.created_by,
            "notes": self.notes,
            "config_payload": self.config_payload,
            "published_at": self.published_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkspaceFile(models.Model):
    STORAGE_TENCENT_COS = "tencent_cos"
    STORAGE_RUNTIME_CACHE = "runtime_cache"
    STORAGE_CHOICES = [
        (STORAGE_TENCENT_COS, "腾讯云 COS"),
        (STORAGE_RUNTIME_CACHE, "运行时缓存"),
    ]

    SYNC_PENDING = "pending"
    SYNC_IN_SYNC = "in_sync"
    SYNC_FAILED = "failed"
    SYNC_CHOICES = [
        (SYNC_PENDING, "待同步"),
        (SYNC_IN_SYNC, "已同步"),
        (SYNC_FAILED, "同步失败"),
    ]

    RESOURCE_SYNC_NOT_APPLICABLE = "not_applicable"
    RESOURCE_SYNC_PENDING = "pending"
    RESOURCE_SYNC_INDEXING = "indexing"
    RESOURCE_SYNC_COMPLETED = "completed"
    # Compatibility name for legacy callers. A submitted request is still
    # being indexed; only the Phase 3 worker writes `completed` after waiting.
    RESOURCE_SYNC_SUBMITTED = RESOURCE_SYNC_INDEXING
    RESOURCE_SYNC_FAILED = "failed"
    RESOURCE_SYNC_CHOICES = [
        (RESOURCE_SYNC_NOT_APPLICABLE, "非知识资源"),
        (RESOURCE_SYNC_PENDING, "待提交 OpenViking"),
        (RESOURCE_SYNC_INDEXING, "OpenViking 索引中"),
        (RESOURCE_SYNC_COMPLETED, "OpenViking 索引完成"),
        (RESOURCE_SYNC_FAILED, "OpenViking 同步失败"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="workspace_files")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="workspace_files")
    logical_path = models.CharField(max_length=512)
    file_name = models.CharField(max_length=255)
    storage_backend = models.CharField(max_length=32, choices=STORAGE_CHOICES, default=STORAGE_TENCENT_COS)
    storage_key = models.CharField(max_length=1024)
    checksum = models.CharField(max_length=128, null=True, blank=True)
    mime_type = models.CharField(max_length=128, null=True, blank=True)
    size = models.BigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=1)
    sync_status = models.CharField(max_length=32, choices=SYNC_CHOICES, default=SYNC_PENDING)
    resource_uri = models.CharField(max_length=1024, blank=True, default="")
    resource_sync_status = models.CharField(
        max_length=32,
        choices=RESOURCE_SYNC_CHOICES,
        default=RESOURCE_SYNC_NOT_APPLICABLE,
    )
    resource_task_id = models.CharField(max_length=128, blank=True, default="")
    resource_last_error = models.TextField(blank=True, default="")
    resource_synced_at = models.CharField(max_length=20, blank=True, default="")
    presigned_url_expire_at = models.CharField(max_length=20, null=True, blank=True)
    created_by = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_workspacefile"
        indexes = [
            models.Index(fields=["company", "logical_path"]),
            models.Index(fields=["company", "sync_status"]),
            models.Index(
                fields=["company", "resource_sync_status"],
                name="workspace_company_resource_idx",
            ),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "logical_path": self.logical_path,
            "file_name": self.file_name,
            "storage_backend": self.storage_backend,
            "storage_key": self.storage_key,
            "checksum": self.checksum,
            "mime_type": self.mime_type,
            "size": self.size,
            "version": self.version,
            "sync_status": self.sync_status,
            "resource_uri": self.resource_uri,
            "resource_sync_status": self.resource_sync_status,
            "resource_task_id": self.resource_task_id,
            "resource_last_error": self.resource_last_error,
            "resource_synced_at": self.resource_synced_at,
            "presigned_url_expire_at": self.presigned_url_expire_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkspaceTransferTask(models.Model):
    DIRECTION_PLATFORM_TO_RUNTIME = "platform_to_runtime"
    DIRECTION_RUNTIME_TO_PLATFORM = "runtime_to_platform"
    DIRECTION_CHOICES = [
        (DIRECTION_PLATFORM_TO_RUNTIME, "平台到运行时"),
        (DIRECTION_RUNTIME_TO_PLATFORM, "运行时到平台"),
    ]

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待执行"),
        (STATUS_RUNNING, "执行中"),
        (STATUS_SUCCEEDED, "成功"),
        (STATUS_FAILED, "失败"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="workspace_transfer_tasks")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="workspace_transfer_tasks")
    file = models.ForeignKey(to=WorkspaceFile, on_delete=models.SET_NULL, null=True, blank=True, related_name="transfer_tasks")
    file_logical_path = models.CharField(max_length=512, blank=True, default="")
    file_name = models.CharField(max_length=255, blank=True, default="")
    file_mime_type = models.CharField(max_length=128, blank=True, default="")
    file_size = models.BigIntegerField(default=0)
    direction = models.CharField(max_length=32, choices=DIRECTION_CHOICES)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    request_payload = models.TextField(null=True, blank=True)
    result_payload = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    started_at = models.CharField(max_length=20, null=True, blank=True)
    finished_at = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_workspacetransfertask"
        indexes = [
            models.Index(fields=["company", "direction", "status"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "file_id": self.file_id,
            "file_logical_path": self.file_logical_path,
            "file_name": self.file_name,
            "file_mime_type": self.file_mime_type,
            "file_size": self.file_size,
            "direction": self.direction,
            "status": self.status,
            "request_payload": self.request_payload,
            "result_payload": self.result_payload,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RuntimeRolloutPolicy(models.Model):
    STRATEGY_DIRECT = "direct"
    STRATEGY_CANARY = "canary"
    STRATEGY_CHOICES = [
        (STRATEGY_DIRECT, "鐩存帴鍙戝竷"),
        (STRATEGY_CANARY, "鐏板害鍙戝竷"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_PAUSED = "paused"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "鍚敤"),
        (STATUS_PAUSED, "鏆傚仠"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name="runtime_rollout_policy")
    strategy = models.CharField(max_length=32, choices=STRATEGY_CHOICES, default=STRATEGY_DIRECT)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    canary_percentage = models.IntegerField(default=10)
    auto_promote = models.BooleanField(default=False)
    auto_promote_minutes = models.IntegerField(default=30)
    rollback_on_alert = models.BooleanField(default=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_rolloutpolicy"

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "strategy": self.strategy,
            "status": self.status,
            "canary_percentage": self.canary_percentage,
            "auto_promote": self.auto_promote,
            "auto_promote_minutes": self.auto_promote_minutes,
            "rollback_on_alert": self.rollback_on_alert,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RuntimeAlertPolicy(models.Model):
    CHANNEL_PLATFORM = "platform"
    CHANNEL_EMAIL = "email"
    CHANNEL_WEBHOOK = "webhook"
    CHANNEL_CHOICES = [
        (CHANNEL_PLATFORM, "骞冲彴绔欏唴"),
        (CHANNEL_EMAIL, "閭欢"),
        (CHANNEL_WEBHOOK, "Webhook"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_MUTED = "muted"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "鍚敤"),
        (STATUS_MUTED, "闈欓煶"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name="runtime_alert_policy")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    channels = models.CharField(max_length=255, default=CHANNEL_PLATFORM)
    config_drift_enabled = models.BooleanField(default=True)
    gateway_unhealthy_enabled = models.BooleanField(default=True)
    workspace_sync_failed_enabled = models.BooleanField(default=True)
    delivery_target = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_alertpolicy"

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "status": self.status,
            "channels": [item for item in (self.channels or "").split(",") if item],
            "config_drift_enabled": self.config_drift_enabled,
            "gateway_unhealthy_enabled": self.gateway_unhealthy_enabled,
            "workspace_sync_failed_enabled": self.workspace_sync_failed_enabled,
            "delivery_target": self.delivery_target or "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RuntimeDriftEvent(models.Model):
    STATUS_OPEN = "open"
    STATUS_ACKED = "acked"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_ACKED, "Acknowledged"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    DRIFT_CONFIG = "config"
    DRIFT_GATEWAY = "gateway"
    DRIFT_WORKSPACE = "workspace"
    DRIFT_TYPE_CHOICES = [
        (DRIFT_CONFIG, "Config drift"),
        (DRIFT_GATEWAY, "Gateway drift"),
        (DRIFT_WORKSPACE, "Workspace drift"),
    ]

    RESOLUTION_REPUBLISH = "republish"
    RESOLUTION_ACCEPT_RUNTIME = "accept_runtime"
    RESOLUTION_MANUAL = "manual"
    RESOLUTION_CHOICES = [
        (RESOLUTION_REPUBLISH, "Republish platform config"),
        (RESOLUTION_ACCEPT_RUNTIME, "Accept runtime state"),
        (RESOLUTION_MANUAL, "Manual resolution"),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name="runtime_drift_events")
    instance = models.ForeignKey(to=OpenClawInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="drift_events")
    drift_type = models.CharField(max_length=32, choices=DRIFT_TYPE_CHOICES, default=DRIFT_CONFIG)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_OPEN)
    summary = models.CharField(max_length=255)
    detail = models.TextField(null=True, blank=True)
    resolution = models.CharField(max_length=32, choices=RESOLUTION_CHOICES, null=True, blank=True)
    resolved_by = models.CharField(max_length=128, null=True, blank=True)
    resolved_at = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "runtime_driftevent"
        indexes = [
            models.Index(fields=["company", "drift_type", "status"], name="runtime_dri_company_047aa7_idx"),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "company_id": self.company.cid,
            "instance_id": self.instance_id,
            "drift_type": self.drift_type,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "resolution": self.resolution,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }



