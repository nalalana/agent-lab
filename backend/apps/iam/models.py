import uuid

from django.db import models


class Tenant(models.Model):
    """权限安全边界。个人与企业资源绝不能跨 Tenant 继承授权。"""

    TYPE_PERSONAL = 'personal'
    TYPE_ORGANIZATION = 'organization'
    TYPE_CHOICES = (
        (TYPE_PERSONAL, 'Personal'),
        (TYPE_ORGANIZATION, 'Organization'),
    )
    STATUS_ACTIVE = 'active'
    STATUS_DISABLED = 'disabled'

    id = models.BigAutoField(primary_key=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    external_id = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=255, blank=True, default='')
    owner_user = models.ForeignKey(
        'user.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='owned_iam_tenants',
    )
    super_admin_user = models.ForeignKey(
        'user.User', null=True, blank=True, on_delete=models.PROTECT,
        related_name='super_admin_iam_tenants',
    )
    status = models.CharField(max_length=20, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'iam_tenant'
        indexes = [models.Index(fields=['type', 'status'])]


class TenantMembership(models.Model):
    ROLE_SUPER_ADMIN = 'super_admin'
    ROLE_MEMBER = 'member'
    ROLE_CHOICES = (
        (ROLE_SUPER_ADMIN, 'Super Admin'),
        (ROLE_MEMBER, 'Member'),
    )
    STATUS_ACTIVE = 'active'
    STATUS_DISABLED = 'disabled'

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey('user.User', on_delete=models.CASCADE, related_name='iam_tenant_memberships')
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    status = models.CharField(max_length=20, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'iam_tenant_membership'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'user'], name='iam_tenant_member_uniq'),
            models.UniqueConstraint(
                fields=['tenant'],
                condition=models.Q(role='super_admin', status='active'),
                name='iam_tenant_active_super_admin_uniq',
            ),
        ]


class Group(models.Model):
    """Tenant 内的组织分组。分组只聚合成员，授权仍由 Grant 明确记录。"""

    id = models.BigAutoField(primary_key=True)
    group_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='groups')
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True, default='')
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='children',
    )
    created_by = models.ForeignKey('user.User', null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'iam_group'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'name'], name='iam_group_tenant_name_uniq'),
        ]


class GroupMembership(models.Model):
    id = models.BigAutoField(primary_key=True)
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey('user.User', on_delete=models.CASCADE, related_name='iam_group_memberships')
    created_by = models.ForeignKey(
        'user.User', null=True, on_delete=models.SET_NULL, related_name='created_iam_group_memberships',
    )

    class Meta:
        db_table = 'iam_group_membership'
        constraints = [
            models.UniqueConstraint(fields=['group', 'user'], name='iam_group_member_uniq'),
        ]


class Principal(models.Model):
    """授权主体：User / Group / ServiceAccount 统一抽象。
    采用 (type, external_id) 以支持跨应用引用。
    """
    TYPE_USER = 'user'
    TYPE_GROUP = 'group'
    TYPE_SERVICE = 'service'
    TYPE_AGENT = 'agent'
    TYPE_CHOICES = (
        (TYPE_USER, 'User'),
        (TYPE_GROUP, 'Group'),
        (TYPE_SERVICE, 'ServiceAccount'),
        (TYPE_AGENT, 'Agent'),
    )

    id = models.BigAutoField(primary_key=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    external_id = models.CharField(max_length=128)  # 指向外部实体的唯一标识（如用户ID、组ID）

    class Meta:
        db_table = 'iam_principal'
        unique_together = ('type', 'external_id')


class Resource(models.Model):
    """统一资源目录；所有业务资源通过 tenant 建立安全边界。"""
    TYPE_COMPANY = 'company'
    TYPE_PROJECT = 'project'
    TYPE_TABLE = 'table'
    TYPE_AGENT = 'agent'
    TYPE_KNOWLEDGE_BASE = 'knowledge_base'
    TYPE_CHOICES = (
        (TYPE_COMPANY, 'Company'),
        (TYPE_PROJECT, 'Project'),
        (TYPE_TABLE, 'Table'),
        (TYPE_AGENT, 'Agent'),
        (TYPE_KNOWLEDGE_BASE, 'Knowledge Base'),
    )

    PERMISSION_MODE_INHERIT = 'inherit'
    PERMISSION_MODE_INDEPENDENT = 'independent'
    PERMISSION_MODE_CHOICES = (
        (PERMISSION_MODE_INHERIT, 'Inherit'),
        (PERMISSION_MODE_INDEPENDENT, 'Independent'),
    )

    id = models.BigAutoField(primary_key=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    external_id = models.CharField(max_length=128, unique=True)  # 业务侧唯一ID
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='children')
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name='resources',
    )
    creator = models.ForeignKey(
        'user.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='created_iam_resources',
    )
    permission_mode = models.CharField(
        max_length=20, choices=PERMISSION_MODE_CHOICES, default=PERMISSION_MODE_INHERIT,
    )

    class Meta:
        db_table = 'iam_resource'
        indexes = [
            models.Index(fields=['type']),
            models.Index(fields=['parent']),
            models.Index(fields=['tenant', 'type']),
        ]


class Grant(models.Model):
    """授权：在某个资源上对某主体授予权限（位标志）。
    权限位：read=1, write=2，admin=3，super_admin=4，可扩展更多位。
    """
    PERM_READ = 1
    PERM_WRITE = 2
    PERM_ADMIN = 3
    PERM_SUPER_ADMIN = 4
    # 资源角色别名。保留旧常量以兼容企业认证和历史调用。
    ROLE_VIEWER = PERM_READ
    ROLE_CONTRIBUTOR = PERM_WRITE
    ROLE_EDITOR = PERM_ADMIN
    ROLE_MANAGER = PERM_SUPER_ADMIN
    PERM_CHOICES = (
        (PERM_READ, 'Read'),
        (PERM_WRITE, 'Write'),
        (PERM_ADMIN, 'Admin'),
        (PERM_SUPER_ADMIN, 'Super Admin'),
    )
    id = models.BigAutoField(primary_key=True)
    principal = models.ForeignKey(Principal, on_delete=models.CASCADE)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE)
    permissions = models.IntegerField(default=0, choices=PERM_CHOICES) 
    granter = models.ForeignKey('user.User', on_delete=models.SET_NULL, null=True)  # 授予者
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    inherit = models.BooleanField(default=True)  # 是否允许向下继承

    class Meta:
        db_table = 'iam_grant'
        indexes = [
            models.Index(fields=['principal', 'resource']),
        ]
        unique_together = ('principal', 'resource')


class ResourceBinding(models.Model):
    """记录项目对 Agent/知识空间的引用；引用本身不会传递 ACL。"""

    id = models.BigAutoField(primary_key=True)
    project_resource = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name='outgoing_bindings',
    )
    resource = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name='incoming_bindings',
    )
    created_by = models.ForeignKey('user.User', null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'iam_resource_binding'
        constraints = [
            models.UniqueConstraint(
                fields=['project_resource', 'resource'], name='iam_resource_binding_uniq',
            ),
        ]


class PermissionAuditLog(models.Model):
    RESULT_SUCCESS = 'success'
    RESULT_DENIED = 'denied'

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, null=True, on_delete=models.SET_NULL, related_name='permission_audits')
    actor = models.ForeignKey(
        'user.User', null=True, on_delete=models.SET_NULL, related_name='permission_audits',
    )
    action = models.CharField(max_length=64)
    resource = models.ForeignKey(Resource, null=True, on_delete=models.SET_NULL)
    target_principal = models.ForeignKey(Principal, null=True, on_delete=models.SET_NULL)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=20, default=RESULT_SUCCESS)
    context = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'iam_permission_audit_log'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['resource', '-created_at']),
        ]
