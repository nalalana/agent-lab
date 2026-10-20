from django.db import models


class RiskRule(models.Model):
    DIMENSION_IP = "ip"
    DIMENSION_PHONE = "phone"
    DIMENSION_USER = "user"
    DIMENSION_API_KEY = "api_key"
    DIMENSION_API_KEY_IP = "api_key_ip"
    DIMENSION_COMPANY = "company"
    DIMENSION_AGENT = "agent"
    DIMENSION_RESOURCE = "resource"
    DIMENSION_USER_AGENT = "user_agent"
    DIMENSION_IP_PHONE = "ip_phone"
    DIMENSION_CHOICES = [
        (DIMENSION_IP, "IP"),
        (DIMENSION_PHONE, "Phone"),
        (DIMENSION_USER, "User"),
        (DIMENSION_API_KEY, "API Key"),
        (DIMENSION_API_KEY_IP, "API Key + IP"),
        (DIMENSION_COMPANY, "Company"),
        (DIMENSION_AGENT, "Agent"),
        (DIMENSION_RESOURCE, "Resource"),
        (DIMENSION_USER_AGENT, "User Agent"),
        (DIMENSION_IP_PHONE, "IP + Phone"),
    ]

    ACTION_RATE_LIMIT = "rate_limit"
    ACTION_FAILURE_LIMIT = "failure_limit"
    ACTION_CHOICES = [
        (ACTION_RATE_LIMIT, "Rate limit"),
        (ACTION_FAILURE_LIMIT, "Failure limit"),
    ]

    id = models.BigAutoField(primary_key=True)
    scope = models.CharField(max_length=128, db_index=True)
    dimension = models.CharField(max_length=32, choices=DIMENSION_CHOICES)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, default=ACTION_RATE_LIMIT)
    limit = models.PositiveIntegerField(default=60)
    window_seconds = models.PositiveIntegerField(default=60)
    block_seconds = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True, default="")
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = "risk_control_rule"
        indexes = [
            models.Index(fields=["scope", "enabled"], name="risk_contro_scope_e_20ecb6_idx"),
            models.Index(fields=["scope", "dimension", "action"], name="risk_contro_scope_d_54beec_idx"),
        ]


class RiskDecisionLog(models.Model):
    DECISION_ALLOW = "allow"
    DECISION_DENY = "deny"
    DECISION_CHOICES = [
        (DECISION_ALLOW, "Allow"),
        (DECISION_DENY, "Deny"),
    ]

    id = models.BigAutoField(primary_key=True)
    scope = models.CharField(max_length=128, db_index=True)
    decision = models.CharField(max_length=16, choices=DECISION_CHOICES)
    reason = models.CharField(max_length=255, blank=True, default="")
    matched_rule = models.CharField(max_length=255, blank=True, default="")
    retry_after = models.PositiveIntegerField(default=0)
    actor_user_id = models.CharField(max_length=128, blank=True, default="")
    company_id = models.CharField(max_length=128, blank=True, default="")
    api_key_id = models.CharField(max_length=128, blank=True, default="")
    resource_id = models.CharField(max_length=128, blank=True, default="")
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    ip_masked = models.CharField(max_length=64, blank=True, default="")
    phone_hash = models.CharField(max_length=64, blank=True, default="")
    user_agent_hash = models.CharField(max_length=64, blank=True, default="")
    request_path = models.CharField(max_length=255, blank=True, default="")
    request_method = models.CharField(max_length=16, blank=True, default="")
    dimensions = models.JSONField(default=dict, blank=True)
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = "risk_control_decision_log"
        indexes = [
            models.Index(fields=["scope", "decision", "created_at"], name="risk_contro_scope_d_6f0291_idx"),
            models.Index(fields=["actor_user_id", "created_at"], name="risk_contro_actor_u_1ef8d9_idx"),
            models.Index(fields=["company_id", "created_at"], name="risk_contro_company_16a9f8_idx"),
            models.Index(fields=["api_key_id", "created_at"], name="risk_contro_api_key_d4ae6f_idx"),
            models.Index(fields=["ip_hash", "created_at"], name="risk_contro_ip_hash_f8b0dc_idx"),
            models.Index(fields=["phone_hash", "created_at"], name="risk_contro_phone_h_7ffb17_idx"),
        ]


class _RiskListEntryBase(models.Model):
    id = models.BigAutoField(primary_key=True)
    dimension = models.CharField(max_length=32, choices=RiskRule.DIMENSION_CHOICES)
    value_hash = models.CharField(max_length=64)
    value_masked = models.CharField(max_length=128, blank=True, default="")
    scope = models.CharField(max_length=128, blank=True, default="")
    reason = models.CharField(max_length=255, blank=True, default="")
    expires_at = models.CharField(max_length=20, blank=True, default="")
    enabled = models.BooleanField(default=True)
    created_by = models.CharField(max_length=128, blank=True, default="")
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        abstract = True


class RiskBlockListEntry(_RiskListEntryBase):
    class Meta(_RiskListEntryBase.Meta):
        db_table = "risk_control_block_list"
        indexes = [
            models.Index(fields=["dimension", "value_hash", "scope", "enabled"], name="risk_contro_dimensi_1c0d5e_idx"),
            models.Index(fields=["expires_at"], name="risk_contro_expires_05cdcf_idx"),
        ]


class RiskAllowListEntry(_RiskListEntryBase):
    multiplier = models.PositiveIntegerField(default=1)

    class Meta(_RiskListEntryBase.Meta):
        db_table = "risk_control_allow_list"
        indexes = [
            models.Index(fields=["dimension", "value_hash", "scope", "enabled"], name="risk_contro_dimensi_d9f651_idx"),
            models.Index(fields=["expires_at"], name="risk_contro_expires_22cb27_idx"),
        ]


class RiskManualAction(models.Model):
    ACTION_BLOCK = "block"
    ACTION_UNBLOCK = "unblock"
    ACTION_ALLOW = "allow"
    ACTION_REMOVE_ALLOW = "remove_allow"
    ACTION_CHOICES = [
        (ACTION_BLOCK, "Block"),
        (ACTION_UNBLOCK, "Unblock"),
        (ACTION_ALLOW, "Allow"),
        (ACTION_REMOVE_ALLOW, "Remove allow"),
    ]

    id = models.BigAutoField(primary_key=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    dimension = models.CharField(max_length=32, choices=RiskRule.DIMENSION_CHOICES)
    value_hash = models.CharField(max_length=64)
    value_masked = models.CharField(max_length=128, blank=True, default="")
    scope = models.CharField(max_length=128, blank=True, default="")
    reason = models.CharField(max_length=255, blank=True, default="")
    operator_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = "risk_control_manual_action"
        indexes = [
            models.Index(fields=["action", "created_at"], name="risk_contro_action_09d066_idx"),
            models.Index(fields=["dimension", "value_hash", "scope"], name="risk_contro_dimensi_48cfb6_idx"),
        ]
