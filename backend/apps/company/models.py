"""
/backend/apps/company/models.py
公司模型
"""
from django.db import models
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Lower, Trim
from apps.opengate.models import Gates
from apps.user.models import User
from apps.runtime.security import sanitize_user_visible_text
from common.utils.Tengcentcos_utils import get_moderated_image_presigned_url, normalize_cos_key
import uuid


# # Create your models here.
class Company(models.Model):
    COMPANY_STATUS_COMMON = 0
    COMPANY_STATUS_TRANSPARENT = 1
    COMPANY_STATUS_CHOICES = [
        (COMPANY_STATUS_COMMON, '普通公司'),
        (COMPANY_STATUS_TRANSPARENT, '透明公司'),
    ]
    VERIFICATION_STATUS_UNVERIFIED = 'unverified'
    VERIFICATION_STATUS_VERIFIED = 'verified'
    VERIFICATION_STATUS_CHOICES = [
        (VERIFICATION_STATUS_UNVERIFIED, 'unverified'),
        (VERIFICATION_STATUS_VERIFIED, 'verified'),
    ]
    VERIFICATION_PROVIDER_TENCENT_ESS = 'tencent_ess'
    VERIFICATION_PROVIDER_TENCENT_MANUAL_V1 = 'tencent_manual_v1'
    VERIFICATION_PROVIDER_LEGACY_IMPORT = 'legacy_import'
    VERIFICATION_PROVIDER_CHOICES = [
        (VERIFICATION_PROVIDER_TENCENT_ESS, 'tencent_ess'),
        (VERIFICATION_PROVIDER_TENCENT_MANUAL_V1, 'tencent_manual_v1'),
        (VERIFICATION_PROVIDER_LEGACY_IMPORT, 'legacy_import'),
    ]

    id = models.BigAutoField(primary_key=True)
    # Creation provenance only; never use this field as ownership or authorization.
    account = models.ForeignKey(to=User, null=True, blank=True, on_delete=models.SET_NULL)
    cid = models.CharField(max_length=40)  # uuid
    logo = models.CharField(max_length=512, null=True, blank=True)
    background_image = models.CharField(max_length=512, null=True, blank=True)
    followers = models.IntegerField(default=0)
    status = models.CharField(max_length=10, choices=COMPANY_STATUS_CHOICES, null=True)
    company_name = models.CharField(max_length=255)
    description = models.TextField(null=True)
    registration_no = models.CharField(max_length=18, null=True, blank=True)
    establishment_date = models.CharField(null=True, max_length=30)
    company_size = models.CharField(max_length=50, null=True, blank=True)
    contact_phone = models.CharField(max_length=50, null=True, blank=True)
    contact_email = models.EmailField(max_length=255, null=True, blank=True)
    website = models.CharField(max_length=200, null=True, blank=True)
    address = models.CharField(max_length=200, null=True, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default=VERIFICATION_STATUS_UNVERIFIED,
    )
    verification_provider = models.CharField(
        max_length=32,
        choices=VERIFICATION_PROVIDER_CHOICES,
        blank=True,
        default='',
    )
    proxy_organization_open_id = models.CharField(max_length=255, null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_verification = models.ForeignKey(
        to='CompanyVerification',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='verified_companies',
    )
    verification_suspended_at = models.DateTimeField(null=True, blank=True)
    verification_suspension_reason = models.CharField(max_length=64, blank=True, default='')
    service_suspended_at = models.DateTimeField(null=True, blank=True)
    service_suspension_reason = models.CharField(max_length=64, blank=True, default='')
    created_at = models.CharField(null=True, max_length=20)
    updated_at = models.CharField(null=True, max_length=20)

    class Meta:
        db_table = 'company_company'
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(registration_no__isnull=True)
                    | models.Q(registration_no='')
                    | models.Q(registration_no__regex=r'^[0-9ABCDEFGHJKLMNPQRTUWXY]{18}$')
                ),
                name='company_registration_no_canonical_chk',
            ),
            models.UniqueConstraint(
                fields=('registration_no',),
                condition=models.Q(registration_no__isnull=False) & ~models.Q(registration_no=''),
                name='company_registration_no_nonblank_uniq',
            ),
            models.UniqueConstraint(
                fields=('proxy_organization_open_id',),
                condition=(
                    models.Q(proxy_organization_open_id__isnull=False)
                    & ~models.Q(proxy_organization_open_id='')
                ),
                name='company_proxy_org_open_id_nonblank_uniq',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(verification_suspended_at__isnull=True, verification_suspension_reason='')
                    | (
                        models.Q(verification_suspended_at__isnull=False)
                        & ~models.Q(verification_suspension_reason='')
                    )
                ),
                name='company_verification_suspension_pair_chk',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(service_suspended_at__isnull=True, service_suspension_reason='')
                    | (
                        models.Q(service_suspended_at__isnull=False)
                        & ~models.Q(service_suspension_reason='')
                    )
                ),
                name='company_service_suspension_pair_chk',
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(
                        verification_status='verified',
                        verification_provider='tencent_manual_v1',
                    )
                    | models.Q(verified_verification__isnull=False)
                ),
                name='company_manual_verified_application_chk',
            ),
        ]

    def get_info(self):
        # 库中可能是历史完整 URL，先规范为 COS key 再生成 presigned URL，避免出现“域名+编码URL”的错误链接
        logo_key = normalize_cos_key(self.logo) if self.logo else None
        bg_key = normalize_cos_key(self.background_image) if self.background_image else None
        upgrade_paid = TransparentCompany.objects.filter(company=self).exists()
        runtime_status = ""
        runtime_openclaw_status = ""
        runtime_config_status = ""
        runtime_engine = ""
        runtime_mode = ""
        runtime_ready = False
        smart_company_status = "common"
        if upgrade_paid:
            from apps.agent_runtime.models import CompanyAgentRuntimePolicy

            policy = CompanyAgentRuntimePolicy.objects.filter(company=self).first()
            runtime_mode = policy.mode if policy else ""
            binding = CompanyVikingBinding.objects.filter(company=self).first()
            runtime_ready = bool(
                policy
                and policy.mode == CompanyAgentRuntimePolicy.MODE_UNIFIED
                and binding
                and binding.status == CompanyVikingBinding.STATUS_ACTIVE
            )
            runtime_engine = "langgraph" if runtime_ready else ""
            runtime_status = "running" if runtime_ready else "configuration_required"
            runtime_config_status = "active" if runtime_ready else "pending"
            smart_company_status = "ready" if runtime_ready else "runtime_pending"
        is_transparent = upgrade_paid
        return {
            'cid': self.cid,
            'logo': get_moderated_image_presigned_url(logo_key) if logo_key else None,
            'background_image': get_moderated_image_presigned_url(bg_key) if bg_key else None,
            'followers': self.followers,
            'account': self.account.get_info() if self.account else None,
            'status': self.status,
            'is_transparent': is_transparent,
            'upgrade_paid': upgrade_paid,
            'runtime_ready': runtime_ready,
            'runtime_status': runtime_status,
            'runtime_engine': runtime_engine,
            'runtime_mode': runtime_mode,
            'runtime_openclaw_status': runtime_openclaw_status,
            'runtime_config_status': runtime_config_status,
            'smart_company_status': smart_company_status,
            'company_name': self.company_name,
            'description': self.description,
            'registration_no': self.registration_no,
            'verification_status': self.verification_status,
            'verification_provider': self.verification_provider,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            'verification_suspended': self.verification_suspended_at is not None,
            'verification_revoked': self.verification_suspended_at is not None,
            'service_suspended': self.service_suspended_at is not None,
            'service_suspended_at': self.service_suspended_at.isoformat() if self.service_suspended_at else None,
            'establishment_date': self.establishment_date,
            'company_size': self.company_size,
            'contact_phone': self.contact_phone,
            'contact_email': self.contact_email,
            'website': self.website,
            'address': self.address,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyScopedAdmin(models.Model):
    """Human administrator with narrowly delegated knowledge and Agent access.

    This is deliberately not an IAM ADMIN grant: those grants authorize many
    unrelated company settings and must never be minted by this feature.
    """

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='scoped_admins')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='scoped_company_admins')
    role_name = models.CharField(max_length=100, default='受限管理员')
    file_rules = models.JSONField(default=list, blank=True)
    agent_ids = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'company_scoped_admin'
        constraints = [models.UniqueConstraint(fields=('company', 'user'), name='company_scoped_admin_user_uniq')]


class ProtectedHistoryQuerySet(models.QuerySet):
    def delete(self):
        raise ProtectedError(
            "Enterprise verification history is append-only",
            self,
        )


class ProtectedHistoryManager(models.Manager.from_queryset(ProtectedHistoryQuerySet)):
    pass


class ProtectedHistoryModel(models.Model):
    objects = ProtectedHistoryManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        raise ProtectedError(
            "Enterprise verification history is append-only",
            {self},
        )


class CompanyVerification(ProtectedHistoryModel):
    PROVIDER_TENCENT_ESS = 'tencent_ess'
    PROVIDER_TENCENT_MANUAL_V1 = 'tencent_manual_v1'
    PROVIDER_CHOICES = [
        (PROVIDER_TENCENT_ESS, 'tencent_ess'),
        (PROVIDER_TENCENT_MANUAL_V1, 'tencent_manual_v1'),
    ]

    ENVIRONMENT_LEGACY = 'legacy'
    ENVIRONMENT_SANDBOX = 'sandbox'
    ENVIRONMENT_PRODUCTION = 'production'
    ENVIRONMENT_MOCK = 'mock'
    ENVIRONMENT_CHOICES = [
        (ENVIRONMENT_LEGACY, 'legacy'),
        (ENVIRONMENT_SANDBOX, 'sandbox'),
        (ENVIRONMENT_PRODUCTION, 'production'),
        (ENVIRONMENT_MOCK, 'mock'),
    ]

    MODE_TWO_ELEMENTS = 'two_elements'
    MODE_THREE_ELEMENTS = 'three_elements'
    MODE_FOUR_ELEMENTS = 'four_elements'
    MODE_CHOICES = [
        (MODE_TWO_ELEMENTS, 'two_elements'),
        (MODE_THREE_ELEMENTS, 'three_elements'),
        (MODE_FOUR_ELEMENTS, 'four_elements'),
    ]

    APPLICANT_ROLE_LEGAL_REPRESENTATIVE = 'legal_representative'
    APPLICANT_ROLE_AUTHORIZED_PERSON = 'authorized_person'
    APPLICANT_ROLE_CHOICES = [
        (APPLICANT_ROLE_LEGAL_REPRESENTATIVE, 'legal_representative'),
        (APPLICANT_ROLE_AUTHORIZED_PERSON, 'authorized_person'),
    ]

    STAGE_LEGACY_ESS = 'legacy_ess'
    STAGE_BUSINESS_LICENSE_UPLOAD = 'business_license_upload'
    STAGE_COMPANY_CHECK = 'company_check'
    STAGE_APPLICANT_FACE = 'applicant_face'
    STAGE_AUTHORIZATION_UPLOAD = 'authorization_upload'
    STAGE_DOCUMENT_CHECK = 'document_check'
    STAGE_READY_TO_SUBMIT = 'ready_to_submit'
    STAGE_MANUAL_REVIEW = 'manual_review'
    STAGE_SUPPLEMENT_REQUIRED = 'supplement_required'
    STAGE_COMPLETED = 'completed'
    STAGE_CHOICES = [
        (STAGE_LEGACY_ESS, 'legacy_ess'),
        (STAGE_BUSINESS_LICENSE_UPLOAD, 'business_license_upload'),
        (STAGE_COMPANY_CHECK, 'company_check'),
        (STAGE_APPLICANT_FACE, 'applicant_face'),
        (STAGE_AUTHORIZATION_UPLOAD, 'authorization_upload'),
        (STAGE_DOCUMENT_CHECK, 'document_check'),
        (STAGE_READY_TO_SUBMIT, 'ready_to_submit'),
        (STAGE_MANUAL_REVIEW, 'manual_review'),
        (STAGE_SUPPLEMENT_REQUIRED, 'supplement_required'),
        (STAGE_COMPLETED, 'completed'),
    ]

    STATUS_INITIATING = 'initiating'
    STATUS_PENDING = 'pending'
    STATUS_VERIFIED = 'verified'
    STATUS_REJECTED = 'rejected'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_INITIATING, 'initiating'),
        (STATUS_PENDING, 'pending'),
        (STATUS_VERIFIED, 'verified'),
        (STATUS_REJECTED, 'rejected'),
        (STATUS_EXPIRED, 'expired'),
        (STATUS_CANCELLED, 'cancelled'),
    ]

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    company = models.ForeignKey(
        to=Company,
        on_delete=models.PROTECT,
        related_name='verifications',
    )
    applicant_user = models.ForeignKey(
        to=User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='company_verifications',
    )
    provider = models.CharField(
        max_length=32,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_TENCENT_ESS,
    )
    provider_environment = models.CharField(
        max_length=16,
        choices=ENVIRONMENT_CHOICES,
        default=ENVIRONMENT_LEGACY,
    )
    mode = models.CharField(max_length=24, choices=MODE_CHOICES, blank=True, default='')
    applicant_role = models.CharField(
        max_length=32,
        choices=APPLICANT_ROLE_CHOICES,
        blank=True,
        default='',
    )
    stage = models.CharField(max_length=32, choices=STAGE_CHOICES, default=STAGE_LEGACY_ESS)
    policy_version = models.CharField(max_length=64, default='legacy_ess')
    revision = models.PositiveIntegerField(default=0)
    material_revision = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_revision = models.PositiveIntegerField(null=True, blank=True)
    submitted_material_revision = models.PositiveIntegerField(null=True, blank=True)
    company_name_snapshot = models.CharField(max_length=255, blank=True, default='')
    registration_no_snapshot = models.CharField(max_length=18, blank=True, default='')
    establishment_date_snapshot = models.CharField(max_length=30, blank=True, default='')
    address_snapshot = models.CharField(max_length=200, blank=True, default='')
    applicant_name_snapshot = models.CharField(max_length=128, blank=True, default='')
    applicant_id_type = models.CharField(max_length=32, blank=True, default='')
    applicant_id_encrypted = models.TextField(blank=True, default='')
    applicant_id_masked = models.CharField(max_length=64, blank=True, default='')
    applicant_id_hmac = models.CharField(max_length=64, blank=True, default='')
    applicant_id_key_version = models.CharField(max_length=32, blank=True, default='')
    consent_version = models.CharField(max_length=64, blank=True, default='')
    consented_at = models.DateTimeField(null=True, blank=True)
    application_id = models.CharField(max_length=64, blank=True, default='')
    proxy_organization_open_id = models.CharField(max_length=255, blank=True, default='')
    proxy_operator_open_id = models.CharField(max_length=255, blank=True, default='')
    authorization_id = models.CharField(max_length=255, null=True, blank=True)
    request_id = models.CharField(max_length=255, null=True, blank=True)
    provider_status = models.CharField(max_length=64, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INITIATING)
    failure_reason = models.TextField(blank=True, default='')
    failure_code = models.CharField(max_length=64, blank=True, default='')
    public_reason = models.CharField(max_length=255, blank=True, default='')
    internal_reason = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(auto_now_add=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_companyverification'
        constraints = [
            models.UniqueConstraint(
                fields=('company',),
                condition=models.Q(status__in=('initiating', 'pending')),
                name='company_verification_active_uniq',
            ),
            models.CheckConstraint(
                check=models.Q(revision__gte=0),
                name='company_verification_revision_nonnegative_chk',
            ),
            models.CheckConstraint(
                check=models.Q(material_revision__gte=0),
                name='company_verification_material_revision_nonnegative_chk',
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(provider='tencent_manual_v1')
                    | (
                        ~models.Q(company_name_snapshot='')
                        & ~models.Q(registration_no_snapshot='')
                        & (
                            (
                                ~models.Q(applicant_name_snapshot='')
                                & ~models.Q(applicant_id_encrypted='')
                                & ~models.Q(applicant_id_hmac='')
                            )
                            | (
                                models.Q(applicant_role__in=(
                                    'legal_representative',
                                    'authorized_person',
                                ))
                                & models.Q(applicant_name_snapshot='')
                                & models.Q(applicant_id_encrypted='')
                                & models.Q(applicant_id_hmac='')
                            )
                        )
                        & ~models.Q(consent_version='')
                        & models.Q(consented_at__isnull=False)
                        & models.Q(expires_at__isnull=False)
                    )
                ),
                name='company_verification_manual_snapshot_chk',
            ),
        ]


class TencentEssOperatorIdentity(models.Model):
    """Stable Tencent employee OpenId for one platform user and ESS application."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        to=User,
        on_delete=models.CASCADE,
        related_name='tencent_ess_operator_identities',
    )
    application_id = models.CharField(max_length=64)
    proxy_operator_open_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_tencentessoperatoridentity'
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'application_id'),
                name='tencent_ess_operator_user_app_uniq',
            ),
            models.UniqueConstraint(
                fields=('application_id', 'proxy_operator_open_id'),
                name='tencent_ess_operator_open_id_app_uniq',
            ),
        ]


class TencentEssEvent(models.Model):
    PROCESS_STATUS_RECEIVED = 'received'
    PROCESS_STATUS_PROCESSED = 'processed'
    PROCESS_STATUS_IGNORED = 'ignored'
    PROCESS_STATUS_FAILED = 'failed'
    PROCESS_STATUS_CHOICES = [
        (PROCESS_STATUS_RECEIVED, 'received'),
        (PROCESS_STATUS_PROCESSED, 'processed'),
        (PROCESS_STATUS_IGNORED, 'ignored'),
        (PROCESS_STATUS_FAILED, 'failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    application_id = models.CharField(max_length=64)
    msg_id = models.CharField(max_length=255)
    msg_type = models.CharField(max_length=128)
    msg_version = models.CharField(max_length=64, blank=True, default='')
    proxy_organization_open_id = models.CharField(max_length=255, blank=True, default='')
    proxy_operator_open_id = models.CharField(max_length=255, null=True, blank=True)
    authorization_id = models.CharField(max_length=255, null=True, blank=True)
    auth_success = models.BooleanField(null=True, blank=True)
    payload_digest = models.CharField(max_length=64)
    process_status = models.CharField(
        max_length=20,
        choices=PROCESS_STATUS_CHOICES,
        default=PROCESS_STATUS_RECEIVED,
    )
    failure_reason = models.CharField(max_length=255, blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_tencentess_event'
        constraints = [
            models.UniqueConstraint(
                fields=('application_id', 'msg_id'),
                name='tencent_ess_event_app_msg_uniq',
            ),
        ]


class VerificationFaceSession(ProtectedHistoryModel):
    STATUS_CREATED = 'created'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_PASSED = 'passed'
    STATUS_FAILED = 'failed'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_CREATED, 'created'),
        (STATUS_IN_PROGRESS, 'in_progress'),
        (STATUS_PASSED, 'passed'),
        (STATUS_FAILED, 'failed'),
        (STATUS_EXPIRED, 'expired'),
        (STATUS_CANCELLED, 'cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='face_sessions',
    )
    attempt_no = models.PositiveIntegerField()
    provider = models.CharField(max_length=32)
    provider_environment = models.CharField(max_length=16)
    token_encrypted = models.TextField(blank=True, default='')
    token_digest = models.CharField(max_length=64, blank=True, default='')
    rule_id = models.CharField(max_length=128, blank=True, default='')
    request_id = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CREATED)
    outcome_code = models.CharField(max_length=64, blank=True, default='')
    identity_match = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    provider_expires_at = models.DateTimeField(null=True, blank=True)
    query_expires_at = models.DateTimeField(null=True, blank=True)
    last_queried_at = models.DateTimeField(null=True, blank=True)
    result_valid_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_verification_face_session'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'attempt_no'),
                name='verification_face_application_attempt_uniq',
            ),
            models.UniqueConstraint(
                fields=('application',),
                condition=models.Q(status__in=('created', 'in_progress')),
                name='verification_face_active_application_uniq',
            ),
            models.UniqueConstraint(
                fields=('token_digest',),
                condition=~models.Q(token_digest=''),
                name='verification_face_token_digest_uniq',
            ),
        ]


class VerificationDocument(ProtectedHistoryModel):
    TYPE_AUTHORIZATION = 'authorization'
    TYPE_BUSINESS_LICENSE = 'business_license'
    TYPE_CHOICES = [
        (TYPE_AUTHORIZATION, 'authorization'),
        (TYPE_BUSINESS_LICENSE, 'business_license'),
    ]

    SCAN_PENDING = 'pending'
    SCAN_PASSED = 'passed'
    SCAN_FAILED = 'failed'
    SCAN_QUARANTINED = 'quarantined'
    SCAN_STATUS_CHOICES = [
        (SCAN_PENDING, 'pending'),
        (SCAN_PASSED, 'passed'),
        (SCAN_FAILED, 'failed'),
        (SCAN_QUARANTINED, 'quarantined'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='documents',
    )
    material_revision = models.PositiveIntegerField()
    document_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    template_version = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=1024)
    storage_version = models.CharField(max_length=255, blank=True, default='')
    preview_storage_key = models.CharField(max_length=1024, blank=True, default='')
    preview_storage_version = models.CharField(max_length=255, blank=True, default='')
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=128)
    page_count = models.PositiveSmallIntegerField(default=1)
    uploaded_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='verification_documents_uploaded',
    )
    scan_status = models.CharField(
        max_length=20,
        choices=SCAN_STATUS_CHOICES,
        default=SCAN_PENDING,
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_verification_document'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'material_revision', 'document_type'),
                name='verification_document_material_type_uniq',
            ),
            models.CheckConstraint(
                check=models.Q(page_count__gte=1),
                name='verification_document_page_count_positive_chk',
            ),
            models.CheckConstraint(
                check=models.Q(material_revision__gte=1),
                name='verification_document_revision_positive_chk',
            ),
            models.UniqueConstraint(
                fields=('storage_key',),
                name='verification_document_storage_key_uniq',
            ),
            models.UniqueConstraint(
                fields=('preview_storage_key',),
                condition=~models.Q(preview_storage_key=''),
                name='verification_document_preview_key_uniq',
            ),
        ]


class VerificationCheck(ProtectedHistoryModel):
    KIND_REGISTRY = 'registry'
    KIND_FACE = 'face'
    KIND_BUSINESS_LICENSE = 'business_license'
    KIND_BUSINESS_LICENSE_SECURITY = 'business_license_security'
    KIND_DOCUMENT_TEXT = 'document_text'
    KIND_SEAL = 'seal'
    KIND_CONSISTENCY = 'consistency'
    KIND_DOCUMENT_SECURITY = 'document_security'
    KIND_CHOICES = [
        (KIND_REGISTRY, 'registry'),
        (KIND_FACE, 'face'),
        (KIND_BUSINESS_LICENSE, 'business_license'),
        (KIND_BUSINESS_LICENSE_SECURITY, 'business_license_security'),
        (KIND_DOCUMENT_TEXT, 'document_text'),
        (KIND_SEAL, 'seal'),
        (KIND_CONSISTENCY, 'consistency'),
        (KIND_DOCUMENT_SECURITY, 'document_security'),
    ]

    STATUS_QUEUED = 'queued'
    STATUS_RUNNING = 'running'
    STATUS_PASSED = 'passed'
    STATUS_FAILED = 'failed'
    STATUS_UNKNOWN = 'unknown'
    STATUS_CHOICES = [
        (STATUS_QUEUED, 'queued'),
        (STATUS_RUNNING, 'running'),
        (STATUS_PASSED, 'passed'),
        (STATUS_FAILED, 'failed'),
        (STATUS_UNKNOWN, 'unknown'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='checks',
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    attempt_no = models.PositiveIntegerField(default=1)
    input_digest = models.CharField(max_length=64)
    material_revision = models.PositiveIntegerField(null=True, blank=True)
    face_session = models.ForeignKey(
        VerificationFaceSession,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='checks',
    )
    document = models.ForeignKey(
        VerificationDocument,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='checks',
    )
    provider = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    mode = models.CharField(max_length=24, blank=True, default='')
    provider_environment = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    provider_code = models.CharField(max_length=128, blank=True, default='')
    request_id = models.CharField(max_length=255, blank=True, default='')
    result = models.JSONField(default=dict, blank=True)
    rule_version = models.CharField(max_length=64)
    checked_at = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_verification_check'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'kind', 'input_digest', 'attempt_no'),
                name='verification_check_input_attempt_uniq',
            ),
        ]


class CompanyVerificationReviewer(ProtectedHistoryModel):
    user = models.OneToOneField(
        User,
        on_delete=models.PROTECT,
        related_name='company_verification_reviewer',
    )
    enabled = models.BooleanField(default=True)
    can_pre_review = models.BooleanField(default=False)
    can_final_review = models.BooleanField(default=False)
    can_suspend_verification = models.BooleanField(default=False)
    reason = models.CharField(max_length=255)
    granted_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='verification_reviewer_grants',
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='verification_reviewer_revocations',
    )

    class Meta:
        db_table = 'company_verification_reviewer'
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(enabled=False)
                    | models.Q(can_pre_review=True)
                    | models.Q(can_final_review=True)
                    | models.Q(can_suspend_verification=True)
                ),
                name='verification_reviewer_enabled_capability_chk',
            ),
        ]


class VerificationReviewerEvent(ProtectedHistoryModel):
    ACTION_GRANTED = 'granted'
    ACTION_UPDATED = 'updated'
    ACTION_REVOKED = 'revoked'
    ACTION_CHOICES = [
        (ACTION_GRANTED, 'granted'),
        (ACTION_UPDATED, 'updated'),
        (ACTION_REVOKED, 'revoked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reviewer = models.ForeignKey(
        CompanyVerificationReviewer,
        on_delete=models.PROTECT,
        related_name='qualification_events',
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='verification_reviewer_events_created',
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    capabilities = models.JSONField(default=list)
    reason = models.CharField(max_length=255)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_verification_reviewer_event'
        indexes = [
            models.Index(
                fields=('reviewer', 'created_at'),
                name='verif_reviewer_event_idx',
            ),
        ]


class VerificationReview(ProtectedHistoryModel):
    ACTION_APPROVE = 'approve'
    ACTION_REJECT = 'reject'
    ACTION_REQUEST_SUPPLEMENT = 'request_supplement'
    ACTION_CHOICES = [
        (ACTION_APPROVE, 'approve'),
        (ACTION_REJECT, 'reject'),
        (ACTION_REQUEST_SUPPLEMENT, 'request_supplement'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='reviews',
    )
    reviewer = models.ForeignKey(
        CompanyVerificationReviewer,
        on_delete=models.PROTECT,
        related_name='reviews',
    )
    action = models.CharField(max_length=24, choices=ACTION_CHOICES)
    submitted_revision = models.PositiveIntegerField()
    material_revision = models.PositiveIntegerField()
    reason_public = models.CharField(max_length=255, blank=True, default='')
    reason_internal = models.TextField(blank=True, default='')
    checklist = models.JSONField(default=dict)
    status_before = models.CharField(max_length=20)
    status_after = models.CharField(max_length=20)
    checks = models.ManyToManyField(VerificationCheck, related_name='reviews', blank=True)
    documents = models.ManyToManyField(VerificationDocument, related_name='reviews', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_verification_review'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'submitted_revision'),
                name='verification_review_application_revision_uniq',
            ),
        ]


class VerificationTranscription(ProtectedHistoryModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='transcriptions',
    )
    document = models.ForeignKey(
        VerificationDocument,
        on_delete=models.PROTECT,
        related_name='transcriptions',
    )
    reviewer = models.ForeignKey(
        CompanyVerificationReviewer,
        on_delete=models.PROTECT,
        related_name='transcriptions',
    )
    source_check = models.ForeignKey(
        VerificationCheck,
        on_delete=models.PROTECT,
        related_name='source_transcriptions',
    )
    result_check = models.OneToOneField(
        VerificationCheck,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='result_transcription',
    )
    application_revision = models.PositiveIntegerField()
    material_revision = models.PositiveIntegerField()
    corrections = models.JSONField(default=list)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_verification_transcription'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'material_revision'),
                name='verification_transcription_material_uniq',
            ),
        ]


class VerificationOperation(ProtectedHistoryModel):
    KIND_START = 'start'
    KIND_CANCEL = 'cancel'
    KIND_EXPIRE = 'expire'
    KIND_TRANSITION = 'transition'
    KIND_APPLICANT_IDENTITY = 'applicant_identity'
    KIND_REGISTRY_CHECK = 'registry_check'
    KIND_BUSINESS_LICENSE_OCR = 'business_license_ocr'
    KIND_FACE_START = 'face_start'
    KIND_FACE_QUERY = 'face_query'
    KIND_DOCUMENT_UPLOAD = 'document_upload'
    KIND_DOCUMENT_SCAN = 'document_scan'
    KIND_DOCUMENT_TEXT_OCR = 'document_text_ocr'
    KIND_SEAL_OCR = 'seal_ocr'
    KIND_DOCUMENT_CLEANUP = 'document_cleanup'
    KIND_SUBMIT = 'submit'
    KIND_TRANSCRIPTION = 'transcription'
    KIND_REVIEW_DECISION = 'review_decision'
    KIND_REVIEW_EVIDENCE_REFRESH = 'review_evidence_refresh'
    KIND_SUSPEND_VERIFICATION = 'suspend_verification'
    KIND_SUSPEND_SERVICE = 'suspend_service'
    KIND_RESTORE_VERIFICATION = 'restore_verification'
    KIND_RESTORE_SERVICE = 'restore_service'
    KIND_CHOICES = [
        (KIND_START, 'start'),
        (KIND_CANCEL, 'cancel'),
        (KIND_EXPIRE, 'expire'),
        (KIND_TRANSITION, 'transition'),
        (KIND_APPLICANT_IDENTITY, 'applicant_identity'),
        (KIND_REGISTRY_CHECK, 'registry_check'),
        (KIND_BUSINESS_LICENSE_OCR, 'business_license_ocr'),
        (KIND_FACE_START, 'face_start'),
        (KIND_FACE_QUERY, 'face_query'),
        (KIND_DOCUMENT_UPLOAD, 'document_upload'),
        (KIND_DOCUMENT_SCAN, 'document_scan'),
        (KIND_DOCUMENT_TEXT_OCR, 'document_text_ocr'),
        (KIND_SEAL_OCR, 'seal_ocr'),
        (KIND_DOCUMENT_CLEANUP, 'document_cleanup'),
        (KIND_SUBMIT, 'submit'),
        (KIND_TRANSCRIPTION, 'transcription'),
        (KIND_REVIEW_DECISION, 'review_decision'),
        (KIND_REVIEW_EVIDENCE_REFRESH, 'review_evidence_refresh'),
        (KIND_SUSPEND_VERIFICATION, 'suspend_verification'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'pending'),
        (STATUS_RUNNING, 'running'),
        (STATUS_COMPLETED, 'completed'),
        (STATUS_FAILED, 'failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='verification_operations',
    )
    application = models.ForeignKey(
        CompanyVerification,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='operations',
    )
    operation_kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    application_revision = models.PositiveIntegerField(null=True, blank=True)
    face_attempt_no = models.PositiveIntegerField(null=True, blank=True)
    material_revision = models.PositiveIntegerField(null=True, blank=True)
    document = models.ForeignKey(
        VerificationDocument,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='operations',
    )
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default='')
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_verification_operation'
        constraints = [
            models.UniqueConstraint(
                fields=('company', 'operation_kind', 'idempotency_key'),
                name='verification_operation_scope_key_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=('status', 'next_run_at'), name='verification_operation_due_idx'),
        ]


class VerificationAuditEvent(ProtectedHistoryModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='verification_audit_events',
    )
    application = models.ForeignKey(
        CompanyVerification,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='audit_events',
    )
    actor = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='verification_audit_events',
    )
    action = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=0)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_verification_audit_event'
        indexes = [
            models.Index(fields=('company', 'created_at'), name='verification_audit_company_idx'),
            models.Index(fields=('application', 'created_at'), name='verif_audit_application_idx'),
        ]


class VerificationNotification(ProtectedHistoryModel):
    EVENT_SUBMITTED = 'submitted'
    EVENT_APPROVED = 'approved'
    EVENT_REJECTED = 'rejected'
    EVENT_SUPPLEMENT_REQUIRED = 'supplement_required'
    EVENT_CHOICES = [
        (EVENT_SUBMITTED, 'submitted'),
        (EVENT_APPROVED, 'approved'),
        (EVENT_REJECTED, 'rejected'),
        (EVENT_SUPPLEMENT_REQUIRED, 'supplement_required'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_SENDING = 'sending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'pending'),
        (STATUS_SENDING, 'sending'),
        (STATUS_SENT, 'sent'),
        (STATUS_FAILED, 'failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        CompanyVerification,
        on_delete=models.PROTECT,
        related_name='notifications',
    )
    recipient = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='company_verification_notifications',
    )
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES)
    application_revision = models.PositiveIntegerField()
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_verification_notification'
        constraints = [
            models.UniqueConstraint(
                fields=('application', 'event_type', 'application_revision'),
                name='verification_notification_event_revision_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=('status', 'next_run_at'),
                name='verif_notification_due_idx',
            ),
        ]


class CompanyClaim(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'pending'),
        (STATUS_APPROVED, 'approved'),
        (STATUS_REJECTED, 'rejected'),
        (STATUS_EXPIRED, 'expired'),
        (STATUS_CANCELLED, 'cancelled'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.PROTECT, related_name='claims')
    applicant_user = models.ForeignKey(
        to=User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='company_claims',
    )
    verification = models.ForeignKey(
        to=CompanyVerification,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='claims',
    )
    proxy_operator_open_id = models.CharField(max_length=255, blank=True, default='')
    authorization_method = models.CharField(max_length=64, blank=True, default='')
    authorization_id = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    failure_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'company_companyclaim'
        constraints = [
            models.UniqueConstraint(
                fields=('company', 'applicant_user'),
                condition=models.Q(status='pending'),
                name='company_claim_pending_applicant_uniq',
            ),
        ]

class companyGate(models.Model):
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE)
    opengate = models.ForeignKey(to=Gates, on_delete=models.CASCADE)

    class Meta:
        db_table = 'company_companygate'
        unique_together = ('company', 'opengate')
        constraints = [models.UniqueConstraint(fields=['opengate'], name='company_gate_single_company_project')]

    def get_info(self):
        return {
            'cid': self.company.cid,
            'gid': self.opengate.gid,
            'opengate_info': self.opengate.get_info(),
        }

class TransparentCompany(models.Model):
    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE)
    avatar = models.CharField(max_length=255, null=True)
    prompts = models.TextField()
    giteeRepoUrl = models.CharField(max_length=255, null=True)
    giteeAdminName = models.CharField(max_length=255, null=True)
    giteeAdminEmail = models.CharField(max_length=255, null=True)

    class Meta:
        db_table = 'company_transparentcompany'

    def get_info(self):
        return {
            'cid': self.company.cid,
            'company': self.company.get_info(),
            'avatar': get_moderated_image_presigned_url(self.avatar) if self.avatar else None,
            'prompts': self.prompts,
        }


class TransparentCompanySkill(models.Model):
    """
    透明公司 Skill 表

    对应 repo 中每个 skill 目录下的 `skill.yaml` + `prompt.md`：
    - skill.yaml  -> 元信息字段
    - prompt.md   -> content 字段
    """

    CATEGORY_DEFAULT = '通用'
    CATEGORY_OPTIONS = (
        CATEGORY_DEFAULT,
        '金融',
        '法律',
        '新媒体创作',
        '办公写作',
        '电商/营销',
        '出行/生活',
        '医疗',
        '编程',
        '学术',
    )

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_DEPRECATED = 'deprecated'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
        (STATUS_DEPRECATED, 'deprecated'),
    ]
    SOURCE_REPO = 'repo'
    SOURCE_MANUAL = 'manual'
    SOURCE_SYSTEM = 'system'
    SOURCE_CHOICES = [
        (SOURCE_REPO, 'repo'),
        (SOURCE_MANUAL, 'manual'),
        (SOURCE_SYSTEM, 'system'),
    ]
    RUNTIME_STATUS_PENDING = 'pending'
    RUNTIME_STATUS_ACTIVE = 'active'
    RUNTIME_STATUS_ERROR = 'error'
    RUNTIME_STATUS_CHOICES = [
        (RUNTIME_STATUS_PENDING, 'pending'),
        (RUNTIME_STATUS_ACTIVE, 'active'),
        (RUNTIME_STATUS_ERROR, 'error'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(to=TransparentCompany, on_delete=models.CASCADE)
    # 对应 skill.yaml
    skillId = models.CharField(max_length=100)  # skill_id
    name = models.CharField(max_length=255)  # name
    version = models.CharField(max_length=20, default='1.0.0')  # version
    category = models.CharField(max_length=50, default=CATEGORY_DEFAULT)  # category
    priority = models.IntegerField(default=10)  # priority
    enabled = models.BooleanField(default=True)  # enabled
    shortDescription = models.CharField(max_length=255, null=True, blank=True)  # short_description
    author = models.CharField(max_length=255, null=True, blank=True)  # author
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)  # status
    sourceType = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_REPO)
    runtimeStatus = models.CharField(max_length=20, choices=RUNTIME_STATUS_CHOICES, default=RUNTIME_STATUS_PENDING)
    isSystemSkill = models.BooleanField(default=False)
    lastPublishedAt = models.CharField(max_length=20, null=True, blank=True)

    # 对应 prompt.md，完整的 skill 说明 / 提示词
    content = models.TextField(null=True, blank=True)

    # 记录最近一次从 repo 同步的时间（时间戳字符串，保持与其它表一致风格）
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyskill'
        unique_together = ('transparentCompany', 'skillId')

    def get_info(self):
        return {
            'id': self.id,
            'transparentCompany': self.transparentCompany.get_info(),
            'skillId': self.skillId,
            'name': self.name,
            'version': self.version,
            'category': self.category,
            'priority': self.priority,
            'enabled': self.enabled,
            'shortDescription': self.shortDescription,
            'author': self.author,
            'status': self.status,
            'sourceType': self.sourceType,
            'runtimeStatus': self.runtimeStatus,
            'isSystemSkill': self.isSystemSkill,
            'content': self.content,
            'updated_at': self.updated_at,
            'lastPublishedAt': self.lastPublishedAt,
        }


class TransparentCompanySkillApiCall(models.Model):
    """
    透明公司 Skill API 调用表

    对应每个 skill 下的 `apis.yaml` 中的内容，
    一个 Skill 可以有多个 API 调用配置。
    """
    id = models.BigAutoField(primary_key=True)
    skill = models.ForeignKey(to=TransparentCompanySkill, on_delete=models.CASCADE, related_name='apiCalls')

    apiName = models.CharField(max_length=255)          # api_name
    method = models.CharField(max_length=10)            # HTTP 方法，如 GET / POST
    url = models.CharField(max_length=512)              # 请求 URL
    description = models.TextField(null=True, blank=True)  # 接口说明
    authType = models.CharField(max_length=50, null=True, blank=True)  # auth_type
    timeoutSeconds = models.IntegerField(default=5)     # timeout_seconds
    retryCount = models.IntegerField(default=0)         # retry_count
    enabled = models.BooleanField(default=True)         # enabled

    class Meta:
        db_table = 'company_transparentcompanyskillapicall'
        unique_together = ('skill', 'apiName')

    def get_info(self):
        return {
            'id': self.id,
            'skillId': self.skill.skillId,
            'apiName': self.apiName,
            'method': self.method,
            'url': self.url,
            'description': self.description,
            'authType': self.authType,
            'timeoutSeconds': self.timeoutSeconds,
            'retryCount': self.retryCount,
            'enabled': self.enabled,
        }


class TransparentCompanySkillDependency(models.Model):
    """
    透明公司 Skill 依赖关系表

    用于描述 Skill 之间的依赖，例如：
    - data_query 依赖某些 _shared 配置
    - policy_explain 依赖 data_query 的结果等
    """
    TYPE_REQUIRES = 'requires'
    TYPE_USES_SHARED = 'uses_shared'
    TYPE_CHOICES = [
        (TYPE_REQUIRES, 'requires'),
        (TYPE_USES_SHARED, 'uses_shared'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(to=TransparentCompany, on_delete=models.CASCADE)
    # 依赖关系：fromSkill 依赖 toSkill
    fromSkill = models.ForeignKey(
        to=TransparentCompanySkill,
        on_delete=models.CASCADE,
        related_name='dependencies',
    )
    toSkill = models.ForeignKey(
        to=TransparentCompanySkill,
        on_delete=models.CASCADE,
        related_name='dependents',
    )
    depType = models.CharField(max_length=32, choices=TYPE_CHOICES, default=TYPE_REQUIRES)
    note = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = 'company_transparentcompanyskilldependency'
        unique_together = ('transparentCompany', 'fromSkill', 'toSkill', 'depType')

    def get_info(self):
        return {
            'id': self.id,
            'transparentCompany': self.transparentCompany.get_info(),
            'fromSkillId': self.fromSkill.skillId,
            'toSkillId': self.toSkill.skillId,
            'depType': self.depType,
            'note': self.note,
        }


class TransparentCompanySkillSyncLog(models.Model):
    """
    透明公司 Skill 同步日志表

    Webhook / 定时任务每次同步 manifest + skills 后，记录一条日志：
    - 方便审计 / 排错 / 回滚
    """
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_PARTIAL = 'partial'
    STATUS_CHOICES = [
        (STATUS_SUCCESS, 'success'),
        (STATUS_FAILED, 'failed'),
        (STATUS_PARTIAL, 'partial'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(to=TransparentCompany, on_delete=models.CASCADE)

    # 本次同步关联的仓库信息
    repoFullName = models.CharField(max_length=255)          # 如 org/company-xxxx
    manifestVersion = models.CharField(max_length=64, null=True, blank=True)  # manifest 里的 version

    # 同步结果
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SUCCESS)
    errorMessage = models.TextField(null=True, blank=True)

    # 变更概要（JSON 字符串，方便后续扩展：新增/更新/删除了哪些 skill / api）
    changedSkills = models.TextField(null=True, blank=True)

    # manifest / skills 的快照（可选，用于审计 / 排查）
    manifestSnapshot = models.TextField(null=True, blank=True)

    # 与其它表保持一致，使用字符串存时间
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyskillsynclog'
        ordering = ['-id']

    def get_info(self):
        return {
            'id': self.id,
            'transparentCompany': self.transparentCompany.get_info(),
            'repoFullName': self.repoFullName,
            'manifestVersion': self.manifestVersion,
            'status': self.status,
            'errorMessage': self.errorMessage,
            'changedSkills': self.changedSkills,
            'created_at': self.created_at,
        }


class TransparentCompanyAgentDefinition(models.Model):
    """
    V2 平台侧 Agent 定义。

    Agent 配置由平台侧统一管理，
    后续由 runtime 配置装配服务写入 OpenClaw。
    """

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_DEPRECATED = 'deprecated'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
        (STATUS_DEPRECATED, 'deprecated'),
    ]

    ROLE_MAIN_AGENT = 'main_agent'
    ROLE_SUBAGENT = 'subagent'
    ROLE_CHOICES = [
        (ROLE_MAIN_AGENT, 'main_agent'),
        (ROLE_SUBAGENT, 'subagent'),
    ]

    VISIBILITY_PUBLIC = 'public'
    VISIBILITY_INTERNAL = 'internal'
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, 'public'),
        (VISIBILITY_INTERNAL, 'internal'),
    ]

    CLIENT_VISIBILITY_VISIBLE = 'visible'
    CLIENT_VISIBILITY_HIDDEN = 'hidden'
    CLIENT_VISIBILITY_CHOICES = [
        (CLIENT_VISIBILITY_VISIBLE, 'visible'),
        (CLIENT_VISIBILITY_HIDDEN, 'hidden'),
    ]
    CLIENT_VISIBILITY_LISTED = 'listed'
    CLIENT_VISIBILITY_DEFAULT = 'default'

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentDefinitions',
    )
    agentId = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=100, choices=ROLE_CHOICES, default=ROLE_SUBAGENT)
    description = models.TextField(null=True, blank=True)
    systemPrompt = models.TextField(null=True, blank=True)
    accentColor = models.CharField(max_length=7, blank=True, default='')
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_INTERNAL,
    )
    clientVisibility = models.CharField(
        max_length=20,
        choices=CLIENT_VISIBILITY_CHOICES,
        default=CLIENT_VISIBILITY_HIDDEN,
    )
    exposedToClient = models.BooleanField(default=False)
    isDefaultEntry = models.BooleanField(default=False)
    routeKey = models.CharField(max_length=100, null=True, blank=True)
    exposureOrder = models.IntegerField(default=100)
    enabled = models.BooleanField(default=False)
    isCoreAgent = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    sortOrder = models.IntegerField(default=100)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentdefinition'
        unique_together = ('transparentCompany', 'agentId')

    @classmethod
    def normalize_client_visibility(cls, value):
        if value in (None, ''):
            return cls.CLIENT_VISIBILITY_HIDDEN
        normalized = str(value).strip().lower()
        if normalized in (cls.CLIENT_VISIBILITY_HIDDEN,):
            return cls.CLIENT_VISIBILITY_HIDDEN
        if normalized in (
            cls.CLIENT_VISIBILITY_VISIBLE,
            cls.CLIENT_VISIBILITY_LISTED,
            cls.CLIENT_VISIBILITY_DEFAULT,
        ):
            return cls.CLIENT_VISIBILITY_VISIBLE
        return normalized

    def get_client_visibility_display_value(self):
        if bool(self.isDefaultEntry) and bool(self.exposedToClient):
            return self.CLIENT_VISIBILITY_DEFAULT
        if bool(self.exposedToClient):
            return self.CLIENT_VISIBILITY_LISTED
        return self.CLIENT_VISIBILITY_HIDDEN

    def get_info(self, transparent_company_info=None):
        return {
            'id': self.id,
            'transparentCompany': (
                transparent_company_info
                if transparent_company_info is not None
                else self.transparentCompany.get_info()
            ),
            'agentId': self.agentId,
            'name': self.name,
            'role': self.role,
            'description': self.description,
            'systemPrompt': self.systemPrompt,
            'accentColor': self.accentColor,
            'visibility': self.visibility,
            'clientVisibility': self.get_client_visibility_display_value(),
            'exposedToClient': self.exposedToClient,
            'isDefaultEntry': self.isDefaultEntry,
            'routeKey': self.routeKey,
            'exposureOrder': self.exposureOrder,
            'enabled': self.enabled,
            'isCoreAgent': self.isCoreAgent,
            'status': self.status,
            'sortOrder': self.sortOrder,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class TransparentCompanyAgentPromptVersion(models.Model):
    CHANGE_TYPE_FEATURE = 'feature'
    CHANGE_TYPE_FIX = 'fix'
    CHANGE_TYPE_OPTIMIZATION = 'optimization'
    CHANGE_TYPE_SECURITY = 'security'
    CHANGE_TYPE_INITIAL = 'initial'
    CHANGE_TYPE_CHOICES = [
        (CHANGE_TYPE_FEATURE, 'feature'),
        (CHANGE_TYPE_FIX, 'fix'),
        (CHANGE_TYPE_OPTIMIZATION, 'optimization'),
        (CHANGE_TYPE_SECURITY, 'security'),
        (CHANGE_TYPE_INITIAL, 'initial'),
    ]

    id = models.BigAutoField(primary_key=True)
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='promptVersions',
    )
    versionLabel = models.CharField(max_length=32)
    content = models.TextField(blank=True, default='')
    changeReason = models.TextField(blank=True, default='')
    changeType = models.CharField(
        max_length=20,
        choices=CHANGE_TYPE_CHOICES,
        default=CHANGE_TYPE_FEATURE,
    )
    createdBy = models.CharField(max_length=255, blank=True, default='')
    approvedBy = models.CharField(max_length=255, blank=True, default='')
    approvedAt = models.CharField(max_length=50, blank=True, default='')
    isCurrent = models.BooleanField(default=True)
    diffSummary = models.JSONField(default=dict, blank=True)
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentpromptversion'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['agent', '-id'], name='cmp_agent_prompt_idx'),
            models.Index(fields=['agent', 'isCurrent'], name='cmp_agent_prompt_cur_idx'),
        ]

    def get_info(self):
        return {
            'version_id': f'pv-{self.agent.agentId}-{self.id}',
            'version': self.versionLabel,
            'content': self.content,
            'change_reason': self.changeReason,
            'change_type': self.changeType,
            'created_at': self.created_at,
            'created_by': self.createdBy,
            'is_current': self.isCurrent,
            'diff_summary': self.diffSummary or {},
        }


class TransparentCompanyAgentSkillBinding(models.Model):
    """
    V2 平台侧 Agent 与 Skill 绑定关系。

    Agent 可以绑定多个 Skill，但默认都不激活，必须人工开启后
    才会进入 runtime 配置发布链路。
    """

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentSkillBindings',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='skillBindings',
    )
    skill = models.ForeignKey(
        to=TransparentCompanySkill,
        on_delete=models.CASCADE,
        related_name='agentBindings',
    )
    enabled = models.BooleanField(default=False)
    sortOrder = models.IntegerField(default=100)
    note = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentskillbinding'
        unique_together = ('agent', 'skill')

    def get_info(self):
        return {
            'id': self.id,
            'transparentCompany': self.transparentCompany.get_info(),
            'agentId': self.agent.agentId,
            'skillId': self.skill.skillId,
            'enabled': self.enabled,
            'sortOrder': self.sortOrder,
            'note': self.note,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class ModelProvider(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    AUTH_API_KEY = 'api_key'
    AUTH_COMPANY_KEY = 'company_key'
    AUTH_PLATFORM_KEY = 'platform_key'
    AUTH_MODE_CHOICES = [
        (AUTH_API_KEY, 'api_key'),
        (AUTH_COMPANY_KEY, 'company_key'),
        (AUTH_PLATFORM_KEY, 'platform_key'),
    ]

    id = models.BigAutoField(primary_key=True)
    providerId = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=100, unique=True)
    baseUrl = models.CharField(max_length=512, null=True, blank=True)
    authMode = models.CharField(max_length=30, choices=AUTH_MODE_CHOICES, default=AUTH_PLATFORM_KEY)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    isPlatformManaged = models.BooleanField(default=True)
    metadata = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_modelprovider'

    def get_info(self):
        return {
            'id': self.id,
            'providerId': self.providerId,
            'name': self.name,
            'slug': self.slug,
            'baseUrl': self.baseUrl,
            'authMode': self.authMode,
            'status': self.status,
            'isPlatformManaged': self.isPlatformManaged,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class OpenClawVersionCatalog(models.Model):
    id = models.BigAutoField(primary_key=True)
    version = models.CharField(max_length=50, unique=True)
    source = models.CharField(max_length=50, default='openclaw_version_catalog')
    providerCount = models.IntegerField(default=0)
    modelCount = models.IntegerField(default=0)
    packageCount = models.IntegerField(default=0)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)
    lastSyncedAt = models.CharField(max_length=20, null=True, blank=True)

    class Meta:
        db_table = 'company_openclawversioncatalog'

    def get_info(self):
        return {
            'id': self.id,
            'version': self.version,
            'source': self.source,
            'providerCount': self.providerCount,
            'modelCount': self.modelCount,
            'packageCount': self.packageCount,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'lastSyncedAt': self.lastSyncedAt,
        }


class OpenClawVersionProvider(models.Model):
    id = models.BigAutoField(primary_key=True)
    catalog = models.ForeignKey(to=OpenClawVersionCatalog, on_delete=models.CASCADE, related_name='providers')
    providerId = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=100)
    baseUrl = models.CharField(max_length=512, null=True, blank=True)
    authMode = models.CharField(max_length=30, choices=ModelProvider.AUTH_MODE_CHOICES, default=ModelProvider.AUTH_API_KEY)
    status = models.CharField(max_length=20, choices=ModelProvider.STATUS_CHOICES, default=ModelProvider.STATUS_ACTIVE)
    isPlatformManaged = models.BooleanField(default=True)
    metadata = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_openclawversionprovider'
        unique_together = ('catalog', 'providerId')

    def get_info(self):
        return {
            'id': self.id,
            'providerId': self.providerId,
            'name': self.name,
            'slug': self.slug,
            'baseUrl': self.baseUrl,
            'authMode': self.authMode,
            'status': self.status,
            'isPlatformManaged': self.isPlatformManaged,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class OpenClawVersionModel(models.Model):
    id = models.BigAutoField(primary_key=True)
    catalog = models.ForeignKey(to=OpenClawVersionCatalog, on_delete=models.CASCADE, related_name='models')
    provider = models.ForeignKey(to=OpenClawVersionProvider, on_delete=models.CASCADE, related_name='models')
    modelId = models.CharField(max_length=100)
    displayName = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='chat')
    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'active'),
            ('inactive', 'inactive'),
        ],
        default='active',
    )
    supportsTools = models.BooleanField(default=False)
    contextWindow = models.IntegerField(default=0)
    inputPricePer1k = models.FloatField(default=0)
    outputPricePer1k = models.FloatField(default=0)
    isPlatformDefault = models.BooleanField(default=False)
    metadata = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_openclawversionmodel'
        unique_together = ('catalog', 'provider', 'modelId')

    def get_info(self):
        return {
            'id': self.id,
            'providerId': self.provider.providerId,
            'modelId': self.modelId,
            'displayName': self.displayName,
            'category': self.category,
            'status': self.status,
            'supportsTools': self.supportsTools,
            'contextWindow': self.contextWindow,
            'inputPricePer1k': self.inputPricePer1k,
            'outputPricePer1k': self.outputPricePer1k,
            'isPlatformDefault': self.isPlatformDefault,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class OpenClawVersionPackage(models.Model):
    id = models.BigAutoField(primary_key=True)
    catalog = models.ForeignKey(to=OpenClawVersionCatalog, on_delete=models.CASCADE, related_name='packages')
    packageId = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'active'),
            ('inactive', 'inactive'),
        ],
        default='active',
    )
    monthlyTokenQuota = models.BigIntegerField(default=0)
    monthlyRequestQuota = models.BigIntegerField(default=0)
    priority = models.IntegerField(default=100)
    isDefault = models.BooleanField(default=False)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_openclawversionpackage'
        unique_together = ('catalog', 'packageId')

    def get_info(self):
        return {
            'id': self.id,
            'packageId': self.packageId,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'monthlyTokenQuota': self.monthlyTokenQuota,
            'monthlyRequestQuota': self.monthlyRequestQuota,
            'priority': self.priority,
            'isDefault': self.isDefault,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class ModelDefinition(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    provider = models.ForeignKey(to=ModelProvider, on_delete=models.CASCADE, related_name='models')
    modelId = models.CharField(max_length=100)
    displayName = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='chat')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    supportsTools = models.BooleanField(default=False)
    contextWindow = models.IntegerField(default=0)
    inputPricePer1k = models.FloatField(default=0)
    outputPricePer1k = models.FloatField(default=0)
    isPlatformDefault = models.BooleanField(default=False)
    metadata = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_modeldefinition'
        unique_together = ('provider', 'modelId')

    def get_info(self):
        return {
            'id': self.id,
            'providerId': self.provider.providerId,
            'modelId': self.modelId,
            'displayName': self.displayName,
            'category': self.category,
            'status': self.status,
            'supportsTools': self.supportsTools,
            'contextWindow': self.contextWindow,
            'inputPricePer1k': self.inputPricePer1k,
            'outputPricePer1k': self.outputPricePer1k,
            'isPlatformDefault': self.isPlatformDefault,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class ModelPackage(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    packageId = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    monthlyTokenQuota = models.BigIntegerField(default=0)
    monthlyRequestQuota = models.BigIntegerField(default=0)
    priority = models.IntegerField(default=100)
    isDefault = models.BooleanField(default=False)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_modelpackage'

    def get_info(self):
        return {
            'id': self.id,
            'packageId': self.packageId,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'monthlyTokenQuota': self.monthlyTokenQuota,
            'monthlyRequestQuota': self.monthlyRequestQuota,
            'priority': self.priority,
            'isDefault': self.isDefault,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyModelPolicy(models.Model):
    SOURCE_PLATFORM = 'platform'
    SOURCE_BYOK = 'byok'
    SOURCE_HYBRID = 'hybrid'
    SOURCE_CHOICES = [
        (SOURCE_PLATFORM, 'platform'),
        (SOURCE_BYOK, 'byok'),
        (SOURCE_HYBRID, 'hybrid'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name='modelPolicy')
    package = models.ForeignKey(to=ModelPackage, on_delete=models.SET_NULL, null=True, blank=True)
    provider = models.ForeignKey(to=ModelProvider, on_delete=models.SET_NULL, null=True, blank=True)
    defaultModel = models.ForeignKey(to=ModelDefinition, on_delete=models.SET_NULL, null=True, blank=True)
    selectedCredentialName = models.CharField(max_length=255, null=True, blank=True)
    sourceMode = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_PLATFORM)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    rateLimitRpm = models.IntegerField(default=60)
    rateLimitTpm = models.IntegerField(default=120000)
    fallbackToPlatform = models.BooleanField(default=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companymodelpolicy'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'packageId': self.package.packageId if self.package else '',
            'providerId': self.provider.providerId if self.provider else '',
            'defaultModelId': self.defaultModel.modelId if self.defaultModel else '',
            'selectedCredentialName': self.selectedCredentialName or '',
            'sourceMode': self.sourceMode,
            'status': self.status,
            'rateLimitRpm': self.rateLimitRpm,
            'rateLimitTpm': self.rateLimitTpm,
            'fallbackToPlatform': self.fallbackToPlatform,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyApiCredential(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name='apiCredentials')
    provider = models.ForeignKey(to=ModelProvider, on_delete=models.CASCADE, related_name='companyCredentials')
    credentialName = models.CharField(max_length=255)
    keyMasked = models.CharField(max_length=255, null=True, blank=True)
    secretEncrypted = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    lastVerifiedAt = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companyapicredential'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'providerId': self.provider.providerId,
            'credentialName': self.credentialName,
            'keyMasked': self.keyMasked,
            'status': self.status,
            'lastVerifiedAt': self.lastVerifiedAt,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanySessionMemoryPolicy(models.Model):
    MODE_SESSION = 'session'
    MODE_DISABLED = 'disabled'
    MODE_CHOICES = [
        (MODE_SESSION, 'session'),
        (MODE_DISABLED, 'disabled'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    SUMMARY_STYLE_CONCISE = 'concise'
    SUMMARY_STYLE_DETAILED = 'detailed'
    SUMMARY_STYLE_CHOICES = [
        (SUMMARY_STYLE_CONCISE, 'concise'),
        (SUMMARY_STYLE_DETAILED, 'detailed'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name='sessionMemoryPolicy')
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_SESSION)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    enabled = models.BooleanField(default=True)
    recentMessageWindow = models.IntegerField(default=12)
    maxTokens = models.IntegerField(default=4000)
    summaryTriggerTokens = models.IntegerField(default=2800)
    summaryMaxTokens = models.IntegerField(default=800)
    summaryStyle = models.CharField(max_length=20, choices=SUMMARY_STYLE_CHOICES, default=SUMMARY_STYLE_CONCISE)
    sessionTtlDays = models.IntegerField(default=30)
    redactPii = models.BooleanField(default=False)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companysessionmemorypolicy'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'mode': self.mode,
            'status': self.status,
            'enabled': self.enabled,
            'recentMessageWindow': self.recentMessageWindow,
            'maxTokens': self.maxTokens,
            'summaryTriggerTokens': self.summaryTriggerTokens,
            'summaryMaxTokens': self.summaryMaxTokens,
            'summaryStyle': self.summaryStyle,
            'sessionTtlDays': self.sessionTtlDays,
            'redactPii': self.redactPii,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class TransparentCompanyAgentMemoryBinding(models.Model):
    SCOPE_SESSION = 'session'
    SCOPE_DISABLED = 'disabled'
    SCOPE_CHOICES = [
        (SCOPE_SESSION, 'session'),
        (SCOPE_DISABLED, 'disabled'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentMemoryBindings',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='memoryBindings',
    )
    memoryScope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_SESSION)
    enabled = models.BooleanField(default=False)
    inheritCompanyPolicy = models.BooleanField(default=True)
    maxTokensOverride = models.IntegerField(null=True, blank=True)
    summaryStyleOverride = models.CharField(
        max_length=20,
        choices=CompanySessionMemoryPolicy.SUMMARY_STYLE_CHOICES,
        null=True,
        blank=True,
    )
    note = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentmemorybinding'
        unique_together = ('transparentCompany', 'agent')

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.transparentCompany.company.cid,
            'agentId': self.agent.agentId,
            'memoryScope': self.memoryScope,
            'enabled': self.enabled,
            'inheritCompanyPolicy': self.inheritCompanyPolicy,
            'maxTokensOverride': self.maxTokensOverride,
            'summaryStyleOverride': self.summaryStyleOverride,
            'note': self.note,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class OpenVikingCluster(models.Model):
    """One shared OpenViking service; credentials are referenced, never stored."""

    STATUS_ACTIVE = 'active'
    STATUS_DEGRADED = 'degraded'
    STATUS_DISABLED = 'disabled'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_DEGRADED, 'degraded'),
        (STATUS_DISABLED, 'disabled'),
    ]

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=128, unique=True)
    endpoint = models.CharField(max_length=512)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    capacity = models.PositiveIntegerField(default=1000)
    rootCredentialRef = models.CharField(max_length=512)
    isDefault = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'company_openvikingcluster'


class CompanyVikingBinding(models.Model):
    """Durable company/account provisioning state for the shared cluster."""

    STATUS_PENDING = 'pending'
    STATUS_PROVISIONING = 'provisioning'
    STATUS_ACTIVE = 'active'
    STATUS_FAILED = 'failed'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'pending'),
        (STATUS_PROVISIONING, 'provisioning'),
        (STATUS_ACTIVE, 'active'),
        (STATUS_FAILED, 'failed'),
        (STATUS_ARCHIVED, 'archived'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(
        to=Company,
        on_delete=models.CASCADE,
        related_name='vikingBinding',
    )
    cluster = models.ForeignKey(
        to=OpenVikingCluster,
        on_delete=models.PROTECT,
        related_name='companyBindings',
    )
    accountId = models.CharField(max_length=128)
    adminUserId = models.CharField(max_length=128)
    runtimeUserId = models.CharField(max_length=128)
    adminCredentialRef = models.CharField(max_length=512, blank=True, default='')
    runtimeCredentialRef = models.CharField(max_length=512, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    storageQuotaBytes = models.PositiveBigIntegerField(default=10 * 1024 * 1024 * 1024)
    attempts = models.PositiveIntegerField(default=0)
    nextRetryAt = models.DateTimeField(null=True, blank=True)
    provisionedAt = models.DateTimeField(null=True, blank=True)
    lastError = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'company_companyvikingbinding'
        constraints = [
            models.UniqueConstraint(
                fields=('cluster', 'accountId'),
                name='company_viking_cluster_account_unique',
            ),
        ]


class TransparentCompanyAgentVikingSpace(models.Model):
    """Persist the one-to-one OpenViking identity owned by an Agent."""

    STATUS_PENDING = 'pending'
    STATUS_PROVISIONING = 'provisioning'
    STATUS_ACTIVE = 'active'
    STATUS_ARCHIVED = 'archived'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'pending'),
        (STATUS_PROVISIONING, 'provisioning'),
        (STATUS_ACTIVE, 'active'),
        (STATUS_ARCHIVED, 'archived'),
        (STATUS_FAILED, 'failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentVikingSpaces',
    )
    companyBinding = models.ForeignKey(
        to=CompanyVikingBinding,
        on_delete=models.CASCADE,
        related_name='agentSpaces',
        null=True,
        blank=True,
    )
    agent = models.OneToOneField(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='vikingSpace',
    )
    vikingSpaceId = models.CharField(max_length=128, unique=True)
    accountId = models.CharField(max_length=128)
    userId = models.CharField(max_length=128)
    rootUri = models.CharField(max_length=512)
    credentialRef = models.CharField(max_length=512, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    lastError = models.TextField(blank=True, default='')
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentvikingspace'
        unique_together = ('transparentCompany', 'agent')

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.transparentCompany.company.cid,
            'agentId': self.agent.agentId,
            'vikingSpaceId': self.vikingSpaceId,
            'accountId': self.accountId,
            'userId': self.userId,
            'rootUri': self.rootUri,
            'credentialRef': self.credentialRef,
            'status': self.status,
            'lastError': self.lastError,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


# Imported here so Django registers the Phase 1 company prompt model.
from apps.company.prompt_models import TransparentCompanyPromptVersion  # noqa: E402,F401


class CompanyUserMemoryPolicy(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    WRITEBACK_MODE_DISABLED = 'disabled'
    WRITEBACK_MODE_SHADOW = 'shadow'
    WRITEBACK_MODE_CHOICES = [
        (WRITEBACK_MODE_DISABLED, 'disabled'),
        (WRITEBACK_MODE_SHADOW, 'shadow'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name='userMemoryPolicy')
    enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    maxRecallItems = models.IntegerField(default=6)
    maxRecallTokens = models.IntegerField(default=600)
    allowSensitive = models.BooleanField(default=False)
    writebackEnabled = models.BooleanField(default=False)
    writebackMode = models.CharField(
        max_length=20,
        choices=WRITEBACK_MODE_CHOICES,
        default=WRITEBACK_MODE_DISABLED,
    )
    writeMinConfidence = models.FloatField(default=0.9)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companyusermemorypolicy'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'enabled': self.enabled,
            'status': self.status,
            'maxRecallItems': self.maxRecallItems,
            'maxRecallTokens': self.maxRecallTokens,
            'allowSensitive': self.allowSensitive,
            'writebackEnabled': self.writebackEnabled,
            'writebackMode': self.writebackMode,
            'writeMinConfidence': self.writeMinConfidence,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class TransparentCompanyAgentUserMemoryBinding(models.Model):
    SCOPE_DISABLED = 'disabled'
    SCOPE_COMPANY_USER = 'company_user'
    SCOPE_AGENT_USER = 'agent_user'
    SCOPE_CHOICES = [
        (SCOPE_DISABLED, 'disabled'),
        (SCOPE_COMPANY_USER, 'company_user'),
        (SCOPE_AGENT_USER, 'agent_user'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentUserMemoryBindings',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='userMemoryBindings',
    )
    memoryScope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_COMPANY_USER)
    enabled = models.BooleanField(default=False)
    inheritCompanyPolicy = models.BooleanField(default=True)
    maxRecallItemsOverride = models.IntegerField(null=True, blank=True)
    maxRecallTokensOverride = models.IntegerField(null=True, blank=True)
    note = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentusermemorybinding'
        unique_together = ('transparentCompany', 'agent')

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.transparentCompany.company.cid,
            'agentId': self.agent.agentId,
            'memoryScope': self.memoryScope,
            'enabled': self.enabled,
            'inheritCompanyPolicy': self.inheritCompanyPolicy,
            'maxRecallItemsOverride': self.maxRecallItemsOverride,
            'maxRecallTokensOverride': self.maxRecallTokensOverride,
            'note': self.note,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyUserMemoryItem(models.Model):
    SCOPE_COMPANY = 'company'
    SCOPE_AGENT = 'agent'
    SCOPE_CHOICES = [
        (SCOPE_COMPANY, 'company'),
        (SCOPE_AGENT, 'agent'),
    ]

    TYPE_IDENTITY = 'identity'
    TYPE_PREFERENCE = 'preference'
    TYPE_CONSTRAINT = 'constraint'
    TYPE_GOAL = 'goal'
    TYPE_FOLLOWUP = 'followup'
    TYPE_CHOICES = [
        (TYPE_IDENTITY, 'identity'),
        (TYPE_PREFERENCE, 'preference'),
        (TYPE_CONSTRAINT, 'constraint'),
        (TYPE_GOAL, 'goal'),
        (TYPE_FOLLOWUP, 'followup'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_STALE = 'stale'
    STATUS_SUPPRESSED = 'suppressed'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_STALE, 'stale'),
        (STATUS_SUPPRESSED, 'suppressed'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(
        to=Company,
        on_delete=models.CASCADE,
        related_name='userMemoryItems',
    )
    user = models.ForeignKey(
        to=User,
        on_delete=models.CASCADE,
        related_name='companyMemoryItems',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='userMemoryItems',
        null=True,
        blank=True,
    )
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_COMPANY)
    memoryKey = models.CharField(max_length=255)
    memoryType = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_PREFERENCE)
    content = models.TextField(blank=True, default='')
    confidence = models.FloatField(default=1.0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    sourceSession = models.ForeignKey(
        to='TransparentCompanyAgentTestSession',
        on_delete=models.SET_NULL,
        related_name='userMemoryItems',
        null=True,
        blank=True,
    )
    sourceMessageId = models.BigIntegerField(null=True, blank=True)
    lastSeenAt = models.CharField(max_length=20, blank=True, default='')
    lastConfirmedAt = models.CharField(max_length=20, blank=True, default='')
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companyusermemoryitem'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'user_id': self.user_id,
            'agentId': self.agent.agentId if self.agent_id else '',
            'scope': self.scope,
            'memoryKey': self.memoryKey,
            'memoryType': self.memoryType,
            'content': sanitize_user_visible_text(self.content),
            'confidence': self.confidence,
            'status': self.status,
            'sourceSessionId': self.sourceSession_id,
            'sourceMessageId': self.sourceMessageId,
            'lastSeenAt': self.lastSeenAt,
            'lastConfirmedAt': self.lastConfirmedAt,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyUserMemoryWriteLog(models.Model):
    ACTION_CREATE = 'create'
    ACTION_UPDATE = 'update'
    ACTION_SKIP = 'skip'
    ACTION_SUPPRESS = 'suppress'
    ACTION_CHOICES = [
        (ACTION_CREATE, 'create'),
        (ACTION_UPDATE, 'update'),
        (ACTION_SKIP, 'skip'),
        (ACTION_SUPPRESS, 'suppress'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(
        to=Company,
        on_delete=models.CASCADE,
        related_name='userMemoryWriteLogs',
    )
    user = models.ForeignKey(
        to=User,
        on_delete=models.CASCADE,
        related_name='companyMemoryWriteLogs',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='userMemoryWriteLogs',
        null=True,
        blank=True,
    )
    session = models.ForeignKey(
        to='TransparentCompanyAgentTestSession',
        on_delete=models.SET_NULL,
        related_name='userMemoryWriteLogs',
        null=True,
        blank=True,
    )
    sourceMessageId = models.BigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default=ACTION_SKIP)
    candidatePayload = models.JSONField(default=dict, blank=True)
    resultPayload = models.JSONField(default=dict, blank=True)
    created_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companyusermemorywritelog'

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'user_id': self.user_id,
            'agentId': self.agent.agentId if self.agent_id else '',
            'sessionId': self.session_id,
            'sourceMessageId': self.sourceMessageId,
            'action': self.action,
            'candidatePayload': self.candidatePayload,
            'resultPayload': self.resultPayload,
            'created_at': self.created_at,
        }


class TransparentCompanyAgentTestSession(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    SOURCE_AGENT_TEST = 'agent_test'
    SOURCE_PUBLIC_CLIENT_ENTRY = 'public_client_entry'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_COMPLETED, 'completed'),
        (STATUS_FAILED, 'failed'),
    ]
    SOURCE_CHOICES = [
        (SOURCE_AGENT_TEST, 'agent_test'),
        (SOURCE_PUBLIC_CLIENT_ENTRY, 'public_client_entry'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentTestSessions',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='testSessions',
    )
    operator = models.ForeignKey(
        to=User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transparentAgentTestSessions',
    )
    conversationId = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    messageCount = models.IntegerField(default=0)
    lastMessage = models.TextField(blank=True, default='')
    sessionKey = models.CharField(max_length=255, blank=True, default='')
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_AGENT_TEST)
    memorySummary = models.TextField(blank=True, default='')
    memorySummaryMessageCount = models.IntegerField(default=0)
    memorySummaryUpdatedAt = models.CharField(max_length=20, blank=True, default='')
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagenttestsession'
        ordering = ['-id']

    def get_info(self):
        operator_display_name = ''
        if self.operator:
            operator_display_name = (
                getattr(self.operator, 'username', '')
                or getattr(self.operator, 'name', '')
            )
        return {
            'conversation_id': self.conversationId,
            'session_key': self.sessionKey,
            'title': self.title,
            'status': self.status,
            'message_count': self.messageCount,
            'user_name': operator_display_name,
            'user_display_name': operator_display_name,
            'agent_id': self.agent.agentId if self.agent else '',
            'agent_name': self.agent.name if self.agent else '',
            'last_message': self.lastMessage,
            'source': self.source,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class TransparentCompanyAgentTestMessage(models.Model):
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_SYSTEM = 'system'
    ROLE_CHOICES = [
        (ROLE_USER, 'user'),
        (ROLE_ASSISTANT, 'assistant'),
        (ROLE_SYSTEM, 'system'),
    ]

    id = models.BigAutoField(primary_key=True)
    session = models.ForeignKey(
        to=TransparentCompanyAgentTestSession,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_USER)
    content = models.TextField(blank=True, default='')
    tokenCount = models.IntegerField(default=0)
    toolCalls = models.JSONField(default=list, blank=True)
    rawPayload = models.JSONField(default=dict, blank=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagenttestmessage'
        ordering = ['id']

    def get_info(self):
        return {
            'message_id': self.id,
            'role': self.role,
            'content': sanitize_user_visible_text(self.content),
            'token_count': self.tokenCount,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'attachments': __import__('apps.agent_runtime.attachments', fromlist=['public_attachment_metadata']).public_attachment_metadata(
                (self.rawPayload or {}).get('attachments')
            ),
        }


class TransparentCompanyPermissionTemplate(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='permissionTemplates',
    )
    templateId = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    agentType = models.CharField(max_length=100, blank=True, default='')
    skillsInclude = models.JSONField(default=list, blank=True)
    skillsExclude = models.JSONField(default=list, blank=True)
    knowledgeRead = models.JSONField(default=list, blank=True)
    knowledgeWrite = models.JSONField(default=list, blank=True)
    knowledgeDelete = models.JSONField(default=list, blank=True)
    knowledgeDeny = models.JSONField(default=list, blank=True)
    knowledgeReadDeny = models.JSONField(default=list, blank=True)
    knowledgeWriteDeny = models.JSONField(default=list, blank=True)
    knowledgeDeleteDeny = models.JSONField(default=list, blank=True)
    # Tri-state values allow a template to grant, deny, or inherit each memory action.
    memoryRead = models.BooleanField(null=True, blank=True, default=None)
    memoryWrite = models.BooleanField(null=True, blank=True, default=None)
    memoryUpdate = models.BooleanField(null=True, blank=True, default=None)
    memoryDelete = models.BooleanField(null=True, blank=True, default=None)
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanypermissiontemplate'
        unique_together = ('transparentCompany', 'templateId')

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.transparentCompany.company.cid,
            'templateId': self.templateId,
            'name': self.name,
            'description': self.description,
            'agentType': self.agentType,
            'skills': {
                'include': self.skillsInclude,
                'exclude': self.skillsExclude,
            },
            'knowledge': {
                'read': self.knowledgeRead,
                'write': self.knowledgeWrite,
                'delete': self.knowledgeDelete,
                'deny': self.knowledgeDeny,
                'read_deny': self.knowledgeReadDeny,
                'write_deny': self.knowledgeWriteDeny,
                'delete_deny': self.knowledgeDeleteDeny,
            },
            'memory': {
                'read': self.memoryRead,
                'write': self.memoryWrite,
                'update': self.memoryUpdate,
                'delete': self.memoryDelete,
            },
            'enabled': self.enabled,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class TransparentCompanyAgentPermissionBinding(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INACTIVE, 'inactive'),
    ]

    id = models.BigAutoField(primary_key=True)
    transparentCompany = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='agentPermissionBindings',
    )
    agent = models.ForeignKey(
        to=TransparentCompanyAgentDefinition,
        on_delete=models.CASCADE,
        related_name='permissionBindings',
    )
    template = models.ForeignKey(
        to=TransparentCompanyPermissionTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agentBindings',
    )
    skillsInclude = models.JSONField(default=list, blank=True)
    skillsExclude = models.JSONField(default=list, blank=True)
    knowledgeRead = models.JSONField(default=list, blank=True)
    knowledgeWrite = models.JSONField(default=list, blank=True)
    knowledgeDelete = models.JSONField(default=list, blank=True)
    knowledgeDeny = models.JSONField(default=list, blank=True)
    knowledgeReadDeny = models.JSONField(default=list, blank=True)
    knowledgeWriteDeny = models.JSONField(default=list, blank=True)
    knowledgeDeleteDeny = models.JSONField(default=list, blank=True)
    # Agent-level memory switches override template values independently.
    memoryRead = models.BooleanField(null=True, blank=True, default=None)
    memoryWrite = models.BooleanField(null=True, blank=True, default=None)
    memoryUpdate = models.BooleanField(null=True, blank=True, default=None)
    memoryDelete = models.BooleanField(null=True, blank=True, default=None)
    enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INACTIVE)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_transparentcompanyagentpermissionbinding'
        unique_together = ('transparentCompany', 'agent')

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.transparentCompany.company.cid,
            'agentId': self.agent.agentId,
            'templateId': self.template.templateId if self.template else '',
            'templateName': self.template.name if self.template else '自定义',
            'skills': {
                'include': self.skillsInclude,
                'exclude': self.skillsExclude,
            },
            'knowledge': {
                'read': self.knowledgeRead,
                'write': self.knowledgeWrite,
                'delete': self.knowledgeDelete,
                'deny': self.knowledgeDeny,
                'read_deny': self.knowledgeReadDeny,
                'write_deny': self.knowledgeWriteDeny,
                'delete_deny': self.knowledgeDeleteDeny,
            },
            'memory': {
                'read': self.memoryRead,
                'write': self.memoryWrite,
                'update': self.memoryUpdate,
                'delete': self.memoryDelete,
            },
            'enabled': self.enabled,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanyPermissionConfig(models.Model):
    company = models.OneToOneField(
        to=Company,
        on_delete=models.CASCADE,
        related_name='permissionConfig',
        primary_key=True,
        serialize=False,
    )
    configJson = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companypermissionconfig'

    def get_info(self):
        return {
            'cid': self.company.cid,
            'configJson': self.configJson,
            'enabled': self.enabled,
            'updated_at': self.updated_at,
        }


class CompanyMember(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_INVITED = 'invited'
    STATUS_DISABLED = 'disabled'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'active'),
        (STATUS_INVITED, 'invited'),
        (STATUS_DISABLED, 'disabled'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(to=User, on_delete=models.SET_NULL, null=True, blank=True, related_name='companyMemberships')
    memberUid = models.CharField(max_length=255, blank=True, default='')
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=255, blank=True, default='')
    role = models.CharField(max_length=100, default='member')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INVITED)
    lastActiveAt = models.CharField(max_length=50, blank=True, default='')
    invitedByUid = models.CharField(max_length=255, blank=True, default='')
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companymember'
        constraints = [
            models.UniqueConstraint(
                fields=('company', 'user'),
                condition=models.Q(user__isnull=False),
                name='company_member_user_uniq',
            ),
            models.UniqueConstraint(
                Lower(Trim('email')),
                'company',
                condition=models.Q(user__isnull=True),
                name='company_member_invite_email_uniq',
            ),
            models.CheckConstraint(
                check=models.Q(user__isnull=False) | ~models.Q(email__regex=r'^\s*$'),
                name='company_member_identity_required_chk',
            ),
        ]

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        return super().save(*args, **kwargs)

    def get_info(self):
        return {
            'id': self.id,
            'cid': self.company.cid,
            'memberUid': self.memberUid,
            'public_id': self.user.public_id if self.user else None,
            'name': self.name,
            'email': self.email,
            'role': self.role,
            'status': self.status,
            'lastActiveAt': self.lastActiveAt,
            'invitedByUid': self.invitedByUid,
            'user': self.user.get_info() if self.user else None,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class CompanySystemSetting(models.Model):
    company = models.OneToOneField(to=Company, on_delete=models.CASCADE, related_name='systemSetting')
    displayName = models.CharField(max_length=255, blank=True, default='')
    systemDescription = models.TextField(blank=True, default='')
    enablePublicEntry = models.BooleanField(default=True)
    showMonitoringCards = models.BooleanField(default=True)
    requireDiffBeforePublish = models.BooleanField(default=True)
    alertOnDrift = models.BooleanField(default=True)
    alertWebhook = models.CharField(max_length=512, blank=True, default='')
    allowBrowserDirectCosUpload = models.BooleanField(default=True)
    workspacePrefix = models.CharField(max_length=255, blank=True, default='')
    syncTimeoutSeconds = models.IntegerField(default=30)
    defaultEntryAgentId = models.CharField(max_length=100, blank=True, default='')
    defaultModelId = models.CharField(max_length=100, blank=True, default='')
    saveAgentTestSessions = models.BooleanField(default=True)
    notifyPublishSuccess = models.BooleanField(default=True)
    notifyAuditTasks = models.BooleanField(default=True)
    notifyDriftDetected = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_companysystemsetting'

    def get_info(self):
        return {
            'cid': self.company.cid,
            'displayName': self.displayName,
            'systemDescription': self.systemDescription,
            'enablePublicEntry': self.enablePublicEntry,
            'showMonitoringCards': self.showMonitoringCards,
            'requireDiffBeforePublish': self.requireDiffBeforePublish,
            'alertOnDrift': self.alertOnDrift,
            'alertWebhook': self.alertWebhook,
            'allowBrowserDirectCosUpload': self.allowBrowserDirectCosUpload,
            'workspacePrefix': self.workspacePrefix,
            'syncTimeoutSeconds': self.syncTimeoutSeconds,
            'defaultEntryAgentId': self.defaultEntryAgentId,
            'defaultModelId': self.defaultModelId,
            'saveAgentTestSessions': self.saveAgentTestSessions,
            'notifyPublishSuccess': self.notifyPublishSuccess,
            'notifyAuditTasks': self.notifyAuditTasks,
            'notifyDriftDetected': self.notifyDriftDetected,
            'enabled': self.enabled,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
