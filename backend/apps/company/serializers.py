"""
/backend/apps/company/serializers.py
公司序列化器
"""
from rest_framework import serializers

from apps.company.validators import validate_social_credit_code


PHONE_DIGITS_ONLY = r'^\d+$'


def _normalize_knowledge_scope_aliases(attrs):
    """Normalize API aliases without widening read, write, or delete access."""

    aliases = {
        'knowledge_read': (
            'knowledge_read', 'knowledge_readable_scopes',
            'knowledgeRead', 'knowledgeReadableScopes',
        ),
        'knowledge_write': (
            'knowledge_write', 'knowledge_writable_scopes',
            'knowledgeWrite', 'knowledgeWritableScopes',
        ),
        'knowledge_delete': (
            'knowledge_delete', 'knowledge_deletable_scopes',
            'knowledgeDelete', 'knowledgeDeletableScopes',
        ),
        'knowledge_read_deny': ('knowledge_read_deny', 'knowledgeReadDeny'),
        'knowledge_write_deny': ('knowledge_write_deny', 'knowledgeWriteDeny'),
        'knowledge_delete_deny': ('knowledge_delete_deny', 'knowledgeDeleteDeny'),
    }
    for canonical, field_names in aliases.items():
        if not any(field_name in attrs for field_name in field_names):
            continue
        merged = []
        for field_name in field_names:
            for item in attrs.get(field_name) or []:
                if item not in merged:
                    merged.append(item)
        attrs[canonical] = merged
    return attrs


class CompanyCreateSerializer(serializers.Serializer):
    invitation_code = serializers.CharField(max_length=100, write_only=True)
    company_name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    logo = serializers.CharField(required=False, allow_blank=True)
    background_image = serializers.CharField(required=False, allow_blank=True)
    registration_no = serializers.CharField(max_length=32)
    establishment_date = serializers.CharField(required=False, allow_blank=True)
    company_size = serializers.CharField(max_length=50)
    contact_phone = serializers.RegexField(
        regex=PHONE_DIGITS_ONLY,
        max_length=50,
        error_messages={'invalid': '联系电话只能包含数字'},
    )
    contact_email = serializers.EmailField(max_length=255)
    website = serializers.CharField(max_length=200, required=False, allow_blank=True)
    address = serializers.CharField(max_length=200)

    def validate_registration_no(self, value):
        return validate_social_credit_code(value)


class CompanyUpdateSerializer(serializers.Serializer):
    from apps.company.models import Company
    cid = serializers.CharField()
    company_name = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    logo = serializers.CharField(required=False, allow_blank=True)
    background_image = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=Company.COMPANY_STATUS_CHOICES, required=False)
    registration_no = serializers.CharField(max_length=32, required=False, allow_blank=True)
    establishment_date = serializers.CharField(required=False, allow_blank=True)
    company_size = serializers.CharField(max_length=50, required=False, allow_blank=True)
    contact_phone = serializers.RegexField(
        regex=PHONE_DIGITS_ONLY,
        max_length=50,
        required=False,
        allow_blank=True,
        error_messages={'invalid': '联系电话只能包含数字'},
    )
    contact_email = serializers.EmailField(max_length=255, required=False, allow_blank=True)
    website = serializers.CharField(max_length=200, required=False, allow_blank=True)
    address = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_registration_no(self, value):
        return validate_social_credit_code(value, allow_blank=True)


class CompanyQuerySerializer(serializers.Serializer):
    keyword = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class CompanyListByUserSerializer(serializers.Serializer):
    # Kept as an optional compatibility field. The authenticated request user,
    # never this client-provided value, defines whose companies are returned.
    uid = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class CompanyBindGateSerializer(serializers.Serializer):
    company_id = serializers.IntegerField(min_value=1)
    gate_id = serializers.IntegerField(min_value=1)


class CompanyBindProgramSerializer(serializers.Serializer):
    cid = serializers.CharField()
    gid = serializers.CharField()


class CompanyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField()


class CompanyDraftDeleteSerializer(serializers.Serializer):
    cid = serializers.CharField()


class ManualVerificationStartSerializer(serializers.Serializer):
    applicant_role = serializers.ChoiceField(
        choices=['legal_representative', 'authorized_person'],
        required=False,
        allow_blank=True,
        default='',
    )
    applicant_name = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=''
    )
    applicant_id_type = serializers.ChoiceField(
        choices=['resident_identity_card'], required=False, allow_blank=True, default=''
    )
    applicant_id_number = serializers.CharField(
        max_length=64, write_only=True, required=False, allow_blank=True, default=''
    )
    consent_version = serializers.CharField(max_length=64)
    idempotency_key = serializers.CharField(max_length=128)


class ManualVerificationCancelSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)


class ManualVerificationEvidenceSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)

    def validate(self, attrs):
        forbidden = {"token", "biz_token", "success", "mode"}.intersection(self.initial_data)
        if forbidden:
            raise serializers.ValidationError("客户端不得提交核身 token、结果或核验模式")
        return attrs


class ManualVerificationIdentitySerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    applicant_name = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=''
    )
    applicant_id_type = serializers.ChoiceField(
        choices=['resident_identity_card'], default='resident_identity_card'
    )
    applicant_id_number = serializers.CharField(max_length=64, write_only=True)


class ManualVerificationRoleSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    applicant_role = serializers.ChoiceField(
        choices=['legal_representative', 'authorized_person']
    )


class ManualVerificationDocumentUploadSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)

    def validate(self, attrs):
        forbidden = {"storage_key", "file_url", "sha256", "scan_status"}.intersection(
            self.initial_data
        )
        if forbidden:
            raise serializers.ValidationError("客户端不得指定存储键、摘要或扫描结果")
        return attrs


class ManualVerificationSubmitSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)


class VerificationReviewerGrantSerializer(serializers.Serializer):
    target_user_uid = serializers.CharField(max_length=255)
    capabilities = serializers.ListField(
        child=serializers.ChoiceField(
            choices=['pre_review', 'final_review', 'suspend_verification']
        ),
        allow_empty=False,
        max_length=3,
    )
    reason = serializers.CharField(max_length=255)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class VerificationReviewerRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class VerificationReviewerListSerializer(serializers.Serializer):
    limit = serializers.IntegerField(default=100, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class VerificationReviewQueueSerializer(serializers.Serializer):
    queue = serializers.ChoiceField(choices=['pre_review', 'final_review'])
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class VerificationCompanyListSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=['all', 'verified', 'unverified', 'revoked', 'service_suspended'],
        default='all',
    )
    keyword = serializers.CharField(required=False, allow_blank=True, max_length=255)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class VerificationAuditListSerializer(serializers.Serializer):
    category = serializers.ChoiceField(
        choices=['all', 'submission', 'review', 'administration', 'material'],
        default='all',
    )
    keyword = serializers.CharField(required=False, allow_blank=True, max_length=255)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class VerificationTranscriptionCorrectionSerializer(serializers.Serializer):
    field = serializers.ChoiceField(choices=[
        'company_name',
        'registration_no',
        'applicant_name',
        'application_id',
        'template_version',
        'authorization_scope',
        'issued_on',
        'valid_until',
        'seal_company_name',
    ])
    original_value = serializers.CharField(max_length=512)
    corrected_value = serializers.CharField(max_length=512)
    source_location = serializers.CharField(max_length=255)
    reason = serializers.CharField(max_length=500)


class VerificationTranscriptionSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    corrections = VerificationTranscriptionCorrectionSerializer(many=True, allow_empty=False)
    reason = serializers.CharField(max_length=2000)


class VerificationReviewDecisionSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    action = serializers.ChoiceField(choices=['approve', 'reject', 'request_supplement'])
    reason_public = serializers.CharField(max_length=255, required=False, allow_blank=True)
    reason_internal = serializers.CharField(
        max_length=4000, required=False, allow_blank=True, write_only=True
    )
    checklist = serializers.JSONField()
    supplement_fields = serializers.ListField(
        child=serializers.ChoiceField(choices=['authorization_document']),
        required=False,
        allow_empty=True,
        max_length=1,
    )


class VerificationSuspensionSerializer(serializers.Serializer):
    reason_code = serializers.RegexField(regex=r'^[a-z0-9][a-z0-9_-]{2,63}$')
    ticket_reference = serializers.CharField(max_length=128)
    idempotency_key = serializers.CharField(max_length=128)
    expected_application_id = serializers.UUIDField(required=False)


class VerificationRetentionPlanSerializer(serializers.Serializer):
    older_than_seconds = serializers.IntegerField(
        required=False, min_value=86400, max_value=10 * 365 * 86400
    )
    limit = serializers.IntegerField(default=100, min_value=1, max_value=1000)


class CompanyGateListSerializer(serializers.Serializer):
    cid = serializers.CharField()
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)



class CompanyGateDeleteSerializer(serializers.Serializer):
    cid = serializers.CharField()
    gid = serializers.CharField()


class CompanyGateUpdateSerializer(serializers.Serializer):
    company_id = serializers.CharField()
    gate_name = serializers.CharField()
    program_name = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)


class CompanySearchSerializer(serializers.Serializer):
    keyword = serializers.CharField(required=False, allow_blank=True)
    offset = serializers.IntegerField(default=0, min_value=0)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)


class TransparentCompanyCreateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    avatar = serializers.CharField(required=False, allow_blank=True)
    prompts = serializers.CharField()

class TransparentCompanyUpdateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    avatar = serializers.CharField()


class TransparentCompanyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class TransparentCompanyClientEntrySerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class TransparentCompanyClientEntryChatHistoryItemSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "assistant"])
    content = serializers.CharField()


class TransparentCompanyClientEntryChatStreamSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    content = serializers.CharField()
    entry_key = serializers.CharField(max_length=255, required=False, allow_blank=True)
    route_key = serializers.CharField(max_length=255, required=False, allow_blank=True)
    conversation_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    session_key = serializers.CharField(max_length=255, required=False, allow_blank=True)
    messages = TransparentCompanyClientEntryChatHistoryItemSerializer(many=True, required=False)


class TransparentCompanyClientEntryConversationListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class TransparentCompanyClientEntryConversationMessagesSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    conversation_id = serializers.CharField(max_length=255)


class TransparentCompanyMessageSendSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    room_id = serializers.CharField(max_length=255)
    message = serializers.CharField()  # 用户发送的消息


class TransparentCompanySkillListSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanySkill

    cid = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(
        choices=TransparentCompanySkill.STATUS_CHOICES,
        required=False,
    )
    source_type = serializers.ChoiceField(
        choices=TransparentCompanySkill.SOURCE_CHOICES,
        required=False,
    )
    runtime_status = serializers.ChoiceField(
        choices=TransparentCompanySkill.RUNTIME_STATUS_CHOICES,
        required=False,
    )
    enabled = serializers.BooleanField(required=False)


class TransparentCompanySkillToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    skill_id = serializers.CharField(max_length=100)


class TransparentCompanySkillDeleteSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    skill_id = serializers.CharField(max_length=100)


class TransparentCompanySkillUpsertSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanySkill

    cid = serializers.CharField(max_length=255)
    skill_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    version = serializers.CharField(max_length=20, required=False, default='1.0.0')
    category = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        default=TransparentCompanySkill.CATEGORY_DEFAULT,
    )
    priority = serializers.IntegerField(required=False, min_value=0, default=10)
    enabled = serializers.BooleanField(required=False)
    short_description = serializers.CharField(required=False, allow_blank=True)
    author = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=TransparentCompanySkill.STATUS_CHOICES,
        required=False,
        default=TransparentCompanySkill.STATUS_INACTIVE,
    )
    source_type = serializers.ChoiceField(
        choices=TransparentCompanySkill.SOURCE_CHOICES,
        required=False,
        default=TransparentCompanySkill.SOURCE_MANUAL,
    )
    runtime_status = serializers.ChoiceField(
        choices=TransparentCompanySkill.RUNTIME_STATUS_CHOICES,
        required=False,
        default=TransparentCompanySkill.RUNTIME_STATUS_PENDING,
    )
    is_system_skill = serializers.BooleanField(required=False)
    content = serializers.CharField(required=False, allow_blank=True)
    usage = serializers.CharField(required=False, allow_blank=True, max_length=240)
    instructions = serializers.CharField(required=False, allow_blank=True, max_length=1800)
    shell_script = serializers.CharField(required=False, allow_blank=True, max_length=8000)
    shell_script_name = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate_category(self, value):
        return str(value or '').strip() or self.TransparentCompanySkill.CATEGORY_DEFAULT


class TransparentCompanyAgentListSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanyAgentDefinition

    cid = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(
        choices=TransparentCompanyAgentDefinition.STATUS_CHOICES,
        required=False,
    )
    visibility = serializers.ChoiceField(
        choices=TransparentCompanyAgentDefinition.VISIBILITY_CHOICES,
        required=False,
    )
    client_visibility = serializers.CharField(required=False, allow_blank=True)
    exposed_to_client = serializers.BooleanField(required=False)
    is_default_entry = serializers.BooleanField(required=False)
    enabled = serializers.BooleanField(required=False)

    def validate_client_visibility(self, value):
        from apps.company.models import TransparentCompanyAgentDefinition

        normalized = TransparentCompanyAgentDefinition.normalize_client_visibility(value)
        allowed = {
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
        }
        if normalized not in allowed:
            raise serializers.ValidationError('invalid client visibility')
        return normalized


class TransparentCompanyAgentUpsertSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanyAgentDefinition, TransparentCompanyAgentPromptVersion

    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    role = serializers.ChoiceField(
        choices=TransparentCompanyAgentDefinition.ROLE_CHOICES,
        required=False,
        default=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
    )
    description = serializers.CharField(required=False, allow_blank=True)
    system_prompt = serializers.CharField(required=False, allow_blank=True)
    accent_color = serializers.RegexField(regex=r'^#[0-9A-Fa-f]{6}$', required=False)
    visibility = serializers.ChoiceField(
        choices=TransparentCompanyAgentDefinition.VISIBILITY_CHOICES,
        required=False,
        default=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
    )
    client_visibility = serializers.CharField(
        required=False,
        allow_blank=True,
        default=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
    )
    exposed_to_client = serializers.BooleanField(required=False)
    is_default_entry = serializers.BooleanField(required=False)
    route_key = serializers.CharField(max_length=100, required=False, allow_blank=True)
    exposure_order = serializers.IntegerField(required=False, min_value=0)
    enabled = serializers.BooleanField(required=False)
    is_core_agent = serializers.BooleanField(required=False)
    status = serializers.ChoiceField(
        choices=TransparentCompanyAgentDefinition.STATUS_CHOICES,
        required=False,
        default=TransparentCompanyAgentDefinition.STATUS_INACTIVE,
    )
    sort_order = serializers.IntegerField(required=False, min_value=0, default=0)
    version_label = serializers.CharField(required=False, allow_blank=True, max_length=32)
    change_reason = serializers.CharField(required=False, allow_blank=True)
    change_type = serializers.ChoiceField(
        choices=TransparentCompanyAgentPromptVersion.CHANGE_TYPE_CHOICES,
        required=False,
    )

    def validate_client_visibility(self, value):
        from apps.company.models import TransparentCompanyAgentDefinition

        normalized = TransparentCompanyAgentDefinition.normalize_client_visibility(value)
        allowed = {
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
        }
        if normalized not in allowed:
            raise serializers.ValidationError('invalid client visibility')
        return normalized


class TransparentCompanyAgentPromptVersionListSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=100)
    cid = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, default=30, min_value=1, max_value=100)


class TransparentCompanyAgentToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)


class TransparentCompanyAgentReorderItemSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=100)
    exposure_order = serializers.IntegerField(min_value=0)


class TransparentCompanyAgentReorderSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    orders = TransparentCompanyAgentReorderItemSerializer(many=True)


class TransparentCompanyAgentSkillBindingListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    skill_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)


class TransparentCompanyAgentSkillBindingUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)
    skill_id = serializers.CharField(max_length=100)
    enabled = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, min_value=0, default=0)
    note = serializers.CharField(required=False, allow_blank=True)


class TransparentCompanyAgentSkillBindingToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)
    skill_id = serializers.CharField(max_length=100)


class CompanyUpgradeToTransparentSerializer(serializers.Serializer):
    """公司升级为透明公司序列化器"""
    cid = serializers.CharField(max_length=255)
    philosophy = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.CharField(required=False, allow_blank=True)

class TransparentCompanyAgentTestAgentsSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class TransparentCompanyAgentTestSessionsSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)


class TransparentCompanyAgentTestSessionCreateSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)
    title = serializers.CharField(required=False, allow_blank=True)


class TransparentCompanyAgentTestMessagesSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    conversation_id = serializers.CharField(max_length=255)


class TransparentCompanyAgentTestMessageSendSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    conversation_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    content = serializers.CharField(required=False, allow_blank=True)
    attachments = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    title = serializers.CharField(required=False, allow_blank=True)


class TransparentCompanyPermissionTemplateListSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanyPermissionTemplate

    cid = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(
        choices=TransparentCompanyPermissionTemplate.STATUS_CHOICES,
        required=False,
    )
    enabled = serializers.BooleanField(required=False)


class _RejectLegacyToolFieldsSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        errors = {}
        for field_name in ('tools_allow', 'tools_deny', 'toolsAllow', 'toolsDeny', 'tools'):
            if field_name in data:
                errors[field_name] = [
                    '该字段已废弃，平台工具由 LangGraph 统一提供，不能由用户配置。'
                ]
        if errors:
            raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


class TransparentCompanyPermissionTemplateUpsertSerializer(_RejectLegacyToolFieldsSerializer):
    from apps.company.models import TransparentCompanyPermissionTemplate

    cid = serializers.CharField(max_length=255)
    template_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    agent_type = serializers.CharField(max_length=100, required=False, allow_blank=True)
    skills_include = serializers.ListField(child=serializers.CharField(), required=False)
    skills_exclude = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_read = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_write = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_delete = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_readable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_writable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_deletable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeRead = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWrite = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDelete = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeReadableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWritableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDeletableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_read_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_write_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_delete_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeReadDeny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWriteDeny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDeleteDeny = serializers.ListField(child=serializers.CharField(), required=False)
    # Keep all four memory actions optional so partial permission updates remain compatible.
    memory_read = serializers.BooleanField(required=False, allow_null=True)
    memory_write = serializers.BooleanField(required=False, allow_null=True)
    memory_update = serializers.BooleanField(required=False, allow_null=True)
    memory_delete = serializers.BooleanField(required=False, allow_null=True)
    enabled = serializers.BooleanField(required=False)
    status = serializers.ChoiceField(
        choices=TransparentCompanyPermissionTemplate.STATUS_CHOICES,
        required=False,
        default=TransparentCompanyPermissionTemplate.STATUS_ACTIVE,
    )

    def validate(self, attrs):
        return _normalize_knowledge_scope_aliases(attrs)


class TransparentCompanyAgentPermissionBindingListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)
    status = serializers.CharField(required=False, allow_blank=True)


class TransparentCompanyAgentPermissionBindingUpsertSerializer(_RejectLegacyToolFieldsSerializer):
    from apps.company.models import TransparentCompanyAgentPermissionBinding

    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)
    create_only = serializers.BooleanField(required=False, default=False)
    template_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    skills_include = serializers.ListField(child=serializers.CharField(), required=False)
    skills_exclude = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_read = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_write = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_delete = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_readable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_writable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_deletable_scopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeRead = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWrite = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDelete = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeReadableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWritableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDeletableScopes = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_read_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_write_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_delete_deny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeReadDeny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeWriteDeny = serializers.ListField(child=serializers.CharField(), required=False)
    knowledgeDeleteDeny = serializers.ListField(child=serializers.CharField(), required=False)
    # Binding payloads expose memory actions independently from tools and knowledge.
    memory_read = serializers.BooleanField(required=False, allow_null=True)
    memory_write = serializers.BooleanField(required=False, allow_null=True)
    memory_update = serializers.BooleanField(required=False, allow_null=True)
    memory_delete = serializers.BooleanField(required=False, allow_null=True)
    enabled = serializers.BooleanField(required=False)
    status = serializers.ChoiceField(
        choices=TransparentCompanyAgentPermissionBinding.STATUS_CHOICES,
        required=False,
        default=TransparentCompanyAgentPermissionBinding.STATUS_INACTIVE,
    )

    def validate(self, attrs):
        return _normalize_knowledge_scope_aliases(attrs)


class TransparentCompanyAgentPermissionBindingToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)


class CompanyPermissionConfigDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class CompanyPermissionConfigUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    config_json = serializers.JSONField(required=False)
    enabled = serializers.BooleanField(required=False)


class CompanyPermissionWorkspaceTreeSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    path = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    recursive = serializers.BooleanField(required=False, default=False)

    def validate_path(self, value):
        from apps.runtime.policy import normalize_workspace_reference
        from apps.runtime.security import validate_workspace_logical_path

        raw = str(value or "").strip()
        if not raw:
            return ""
        normalized = normalize_workspace_reference(raw)
        if normalized in {"", "*"}:
            return ""
        return validate_workspace_logical_path(normalized, "path")


class ModelProviderListSerializer(serializers.Serializer):
    cid = serializers.CharField(required=False, allow_blank=True)
    from apps.company.models import ModelProvider

    status = serializers.ChoiceField(
        choices=ModelProvider.STATUS_CHOICES,
        required=False,
    )
    auth_mode = serializers.ChoiceField(
        choices=ModelProvider.AUTH_MODE_CHOICES,
        required=False,
    )
    platform_managed = serializers.BooleanField(required=False, allow_null=True, default=None)


class ModelDefinitionListSerializer(serializers.Serializer):
    cid = serializers.CharField(required=False, allow_blank=True)
    from apps.company.models import ModelDefinition

    provider_id = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=ModelDefinition.STATUS_CHOICES,
        required=False,
    )
    category = serializers.CharField(required=False, allow_blank=True)
    supports_tools = serializers.BooleanField(required=False, allow_null=True, default=None)


class ModelPackageListSerializer(serializers.Serializer):
    cid = serializers.CharField(required=False, allow_blank=True)
    from apps.company.models import ModelPackage

    status = serializers.ChoiceField(
        choices=ModelPackage.STATUS_CHOICES,
        required=False,
    )
    is_default = serializers.BooleanField(required=False, allow_null=True, default=None)


class CompanyModelPolicyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class CompanyModelPolicyUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    package_id = serializers.CharField(required=False, allow_blank=True)
    provider_id = serializers.CharField(required=False, allow_blank=True)
    default_model_id = serializers.CharField(required=False, allow_blank=True)
    selected_credential_name = serializers.CharField(required=False, allow_blank=True)
    source_mode = serializers.ChoiceField(
        choices=(('platform', 'platform'),),
        required=False,
        default='platform',
    )
    status = serializers.ChoiceField(
        choices=(
            ('active', 'active'),
            ('inactive', 'inactive'),
            ('deprecated', 'deprecated'),
        ),
        required=False,
        default='active',
    )
    rate_limit_rpm = serializers.IntegerField(required=False, min_value=1, default=60)
    rate_limit_tpm = serializers.IntegerField(required=False, min_value=1, default=120000)
    fallback_to_platform = serializers.BooleanField(required=False, default=True)


class CompanyApiCredentialListSerializer(serializers.Serializer):
    from apps.company.models import CompanyApiCredential

    cid = serializers.CharField(max_length=255)
    provider_id = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=CompanyApiCredential.STATUS_CHOICES,
        required=False,
    )


class CompanyApiCredentialUpsertSerializer(serializers.Serializer):
    from apps.company.models import CompanyApiCredential

    cid = serializers.CharField(max_length=255)
    provider_id = serializers.CharField(max_length=100)
    credential_name = serializers.CharField(max_length=255)
    api_key = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=CompanyApiCredential.STATUS_CHOICES,
        required=False,
        default=CompanyApiCredential.STATUS_INACTIVE,
    )


class CompanySessionMemoryPolicyDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class CompanySessionMemoryPolicyUpsertSerializer(serializers.Serializer):
    from apps.company.models import CompanySessionMemoryPolicy

    cid = serializers.CharField(max_length=255)
    mode = serializers.ChoiceField(
        choices=CompanySessionMemoryPolicy.MODE_CHOICES,
        required=False,
        default=CompanySessionMemoryPolicy.MODE_SESSION,
    )
    status = serializers.ChoiceField(
        choices=CompanySessionMemoryPolicy.STATUS_CHOICES,
        required=False,
        default=CompanySessionMemoryPolicy.STATUS_ACTIVE,
    )
    enabled = serializers.BooleanField(required=False, default=True)
    recent_message_window = serializers.IntegerField(required=False, min_value=1, default=12)
    max_tokens = serializers.IntegerField(required=False, min_value=256, default=4000)
    summary_trigger_tokens = serializers.IntegerField(required=False, min_value=256, default=2800)
    summary_max_tokens = serializers.IntegerField(required=False, min_value=128, default=800)
    summary_style = serializers.ChoiceField(
        choices=CompanySessionMemoryPolicy.SUMMARY_STYLE_CHOICES,
        required=False,
        default=CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
    )
    session_ttl_days = serializers.IntegerField(required=False, min_value=1, default=30)
    redact_pii = serializers.BooleanField(required=False, default=False)


class TransparentCompanyAgentMemoryBindingListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)
    status = serializers.CharField(required=False, allow_blank=True)


class TransparentCompanyAgentMemoryBindingUpsertSerializer(serializers.Serializer):
    from apps.company.models import TransparentCompanyAgentMemoryBinding

    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)
    memory_scope = serializers.ChoiceField(
        choices=TransparentCompanyAgentMemoryBinding.SCOPE_CHOICES,
        required=False,
        default=TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
    )
    enabled = serializers.BooleanField(required=False, default=False)
    inherit_company_policy = serializers.BooleanField(required=False, default=True)
    max_tokens_override = serializers.IntegerField(required=False, min_value=256, allow_null=True)
    summary_style_override = serializers.ChoiceField(
        choices=(
            ('', ''),
            *TransparentCompanyAgentMemoryBinding._meta.get_field('summaryStyleOverride').choices,
        ),
        required=False,
        allow_blank=True,
    )
    note = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=TransparentCompanyAgentMemoryBinding.STATUS_CHOICES,
        required=False,
        default=TransparentCompanyAgentMemoryBinding.STATUS_INACTIVE,
    )


class TransparentCompanyAgentMemoryBindingToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    agent_id = serializers.CharField(max_length=100)


class CompanyControlOverviewSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class CompanyConversationListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    status = serializers.CharField(required=False, allow_blank=True)
    agent_id = serializers.CharField(required=False, allow_blank=True)
    source = serializers.ChoiceField(
        choices=(
            ('', ''),
            ('all', 'all'),
            ('agent_test', 'agent_test'),
            ('public_client_entry', 'public_client_entry'),
        ),
        required=False,
        allow_blank=True,
        default='all',
    )
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class CompanyConversationMessagesSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    conversation_id = serializers.CharField(max_length=255)


class CompanyMembersListSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    status = serializers.CharField(required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)
    keyword = serializers.CharField(required=False, allow_blank=True)


class CompanyMemberUpsertSerializer(serializers.Serializer):
    from apps.company.models import CompanyMember

    cid = serializers.CharField(max_length=255)
    member_id = serializers.IntegerField(required=False)
    member_uid = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    email = serializers.EmailField(required=False, allow_blank=True)
    role = serializers.CharField(max_length=100)
    status = serializers.ChoiceField(
        choices=CompanyMember.STATUS_CHOICES,
        required=False,
        default=CompanyMember.STATUS_INVITED,
    )
    last_active_at = serializers.CharField(required=False, allow_blank=True)


class CompanyMemberToggleStatusSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    member_id = serializers.IntegerField()


class CompanySystemSettingDetailSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)


class CompanySystemSettingUpsertSerializer(serializers.Serializer):
    cid = serializers.CharField(max_length=255)
    display_name = serializers.CharField(required=False, allow_blank=True)
    system_description = serializers.CharField(required=False, allow_blank=True)
    enable_public_entry = serializers.BooleanField(required=False)
    show_monitoring_cards = serializers.BooleanField(required=False)
    require_diff_before_publish = serializers.BooleanField(required=False)
    alert_on_drift = serializers.BooleanField(required=False)
    alert_webhook = serializers.CharField(required=False, allow_blank=True)
    allow_browser_direct_cos_upload = serializers.BooleanField(required=False)
    workspace_prefix = serializers.CharField(required=False, allow_blank=True)
    sync_timeout_seconds = serializers.IntegerField(required=False, min_value=1)
    default_entry_agent_id = serializers.CharField(required=False, allow_blank=True)
    default_model_id = serializers.CharField(required=False, allow_blank=True)
    save_agent_test_sessions = serializers.BooleanField(required=False)
    notify_publish_success = serializers.BooleanField(required=False)
    notify_audit_tasks = serializers.BooleanField(required=False)
    notify_drift_detected = serializers.BooleanField(required=False)
    enabled = serializers.BooleanField(required=False)
