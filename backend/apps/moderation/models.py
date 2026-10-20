import time

from django.db import models


def _now_ts() -> str:
    return str(time.time())[:20]


class ImageModerationAsset(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_REVIEW_REQUIRED = "review_required"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待审核"),
        (STATUS_APPROVED, "审核通过"),
        (STATUS_REJECTED, "审核拒绝"),
        (STATUS_REVIEW_REQUIRED, "需要人工复核"),
        (STATUS_FAILED, "审核失败"),
    ]

    SOURCE_USER_AVATAR = "user.avatar"
    SOURCE_COMPANY_IMAGE = "company.image"
    SOURCE_OPENGATE_IMAGE = "opengate.image"
    SOURCE_OPS_PARTNER_LOGO = "ops.partner.logo"
    SOURCE_RUNTIME_WORKSPACE = "runtime.workspace"

    id = models.BigAutoField(primary_key=True)
    storage_key = models.CharField(max_length=1024, unique=True)
    source = models.CharField(max_length=64)
    source_field = models.CharField(max_length=64, blank=True, default="")
    owner_type = models.CharField(max_length=32, blank=True, default="")
    owner_id = models.CharField(max_length=128, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size = models.BigIntegerField(default=0)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    provider = models.CharField(max_length=32, blank=True, default="")
    provider_job_id = models.CharField(max_length=128, blank=True, default="")
    provider_result = models.JSONField(default=dict, blank=True)
    suggestion = models.CharField(max_length=32, blank=True, default="")
    label = models.CharField(max_length=64, blank=True, default="")
    score = models.FloatField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    reviewed_by = models.CharField(max_length=128, blank=True, default="")
    reviewed_at = models.CharField(max_length=20, blank=True, default="")
    created_at = models.CharField(max_length=20, default=_now_ts)
    updated_at = models.CharField(max_length=20, default=_now_ts)

    class Meta:
        db_table = "moderation_imagemoderationasset"
        indexes = [
            models.Index(fields=["storage_key"]),
            models.Index(fields=["status"]),
            models.Index(fields=["source", "source_field"]),
            models.Index(fields=["owner_type", "owner_id"]),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "storage_key": self.storage_key,
            "source": self.source,
            "source_field": self.source_field,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "mime_type": self.mime_type,
            "size": self.size,
            "status": self.status,
            "provider": self.provider,
            "provider_job_id": self.provider_job_id,
            "suggestion": self.suggestion,
            "label": self.label,
            "score": self.score,
            "failure_reason": self.failure_reason,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TextModerationAsset(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_REVIEW_REQUIRED = "review_required"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_APPROVED, "approved"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_REVIEW_REQUIRED, "review_required"),
        (STATUS_FAILED, "failed"),
    ]

    PROVIDER_DISABLED = "disabled"
    PROVIDER_LOCAL = "local"
    PROVIDER_TENCENT = "tencent"

    SOURCE_COMPANY_PROFILE = "company.profile"
    SOURCE_TRANSPARENT_COMPANY = "company.transparent"
    SOURCE_KNOWLEDGE_FILE = "knowledge.file"
    SOURCE_OPENGATE_PROJECT = "opengate.project"
    SOURCE_OPENGATE_TABLE = "opengate.table"
    SOURCE_EXTERNAL_API_TABLE_UPSERT = "external_api.table_upsert"
    SOURCE_AGENT_PROMPT = "agent.prompt"
    SOURCE_AGENT_TEST_MESSAGE = "agent.test_message"
    SOURCE_PUBLIC_CLIENT_ENTRY = "public_client_entry.message"

    id = models.BigAutoField(primary_key=True)
    content_hash = models.CharField(max_length=64)
    content_preview = models.CharField(max_length=512, blank=True, default="")
    source = models.CharField(max_length=64)
    source_field = models.CharField(max_length=64, blank=True, default="")
    owner_type = models.CharField(max_length=32, blank=True, default="")
    owner_id = models.CharField(max_length=128, blank=True, default="")
    actor_type = models.CharField(max_length=32, blank=True, default="")
    actor_id = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    provider = models.CharField(max_length=32, blank=True, default="")
    provider_result = models.JSONField(default=dict, blank=True)
    suggestion = models.CharField(max_length=32, blank=True, default="")
    label = models.CharField(max_length=64, blank=True, default="")
    score = models.FloatField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    reviewed_by = models.CharField(max_length=128, blank=True, default="")
    reviewed_at = models.CharField(max_length=20, blank=True, default="")
    created_at = models.CharField(max_length=20, default=_now_ts)
    updated_at = models.CharField(max_length=20, default=_now_ts)

    class Meta:
        db_table = "moderation_textmoderationasset"
        indexes = [
            models.Index(fields=["content_hash"], name="moderation__content_b70d38_idx"),
            models.Index(fields=["status"], name="moderation__status_8dd15b_idx"),
            models.Index(fields=["source", "source_field"], name="moderation__source_242ec9_idx"),
            models.Index(fields=["owner_type", "owner_id"], name="moderation__owner_t_28e684_idx"),
            models.Index(fields=["actor_type", "actor_id"], name="moderation__actor_t_bf4d4b_idx"),
        ]

    def get_info(self):
        return {
            "id": self.id,
            "content_hash": self.content_hash,
            "content_preview": self.content_preview,
            "source": self.source,
            "source_field": self.source_field,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "status": self.status,
            "provider": self.provider,
            "suggestion": self.suggestion,
            "label": self.label,
            "score": self.score,
            "failure_reason": self.failure_reason,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TextModerationEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    asset = models.ForeignKey(to=TextModerationAsset, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=64)
    from_status = models.CharField(max_length=32, blank=True, default="")
    to_status = models.CharField(max_length=32, blank=True, default="")
    actor_type = models.CharField(max_length=32, blank=True, default="")
    actor_id = models.CharField(max_length=128, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.CharField(max_length=20, default=_now_ts)

    class Meta:
        db_table = "moderation_textmoderationevent"
        indexes = [
            models.Index(fields=["asset", "created_at"], name="moderation__asset_i_4c5261_idx"),
            models.Index(fields=["event_type"], name="moderation__event_t_1e911d_idx"),
        ]


class SensitiveWordRule(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "active"),
        (STATUS_INACTIVE, "inactive"),
    ]

    ACTION_REJECT = "reject"
    ACTION_REVIEW_REQUIRED = "review_required"
    ACTION_CHOICES = [
        (ACTION_REJECT, "reject"),
        (ACTION_REVIEW_REQUIRED, "review_required"),
    ]

    id = models.BigAutoField(primary_key=True)
    word = models.CharField(max_length=255, unique=True)
    normalized_word = models.CharField(max_length=255, db_index=True)
    label = models.CharField(max_length=64, blank=True, default="sensitive_word")
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, default=ACTION_REJECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_by = models.CharField(max_length=128, blank=True, default="")
    created_at = models.CharField(max_length=20, default=_now_ts)
    updated_at = models.CharField(max_length=20, default=_now_ts)

    class Meta:
        db_table = "moderation_sensitivewordrule"
        indexes = [
            models.Index(fields=["status"], name="moderation__status_053e13_idx"),
            models.Index(fields=["label"], name="moderation__label_2e65cc_idx"),
        ]

    def save(self, *args, **kwargs):
        self.normalized_word = "".join(str(self.word or "").lower().split())
        self.updated_at = _now_ts()
        super().save(*args, **kwargs)
