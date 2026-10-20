from django.db import migrations


def backfill_unified_permissions(apps, schema_editor):
    User = apps.get_model('user', 'User')
    Company = apps.get_model('company', 'Company')
    CompanyVerification = apps.get_model('company', 'CompanyVerification')
    CompanyMember = apps.get_model('company', 'CompanyMember')
    CompanyScopedAdmin = apps.get_model('company', 'CompanyScopedAdmin')
    CompanyGate = apps.get_model('company', 'companyGate')
    TransparentCompany = apps.get_model('company', 'TransparentCompany')
    Agent = apps.get_model('company', 'TransparentCompanyAgentDefinition')
    Gates = apps.get_model('opengate', 'Gates')
    GatesTable = apps.get_model('opengate', 'GatesTable')
    Tenant = apps.get_model('iam', 'Tenant')
    TenantMembership = apps.get_model('iam', 'TenantMembership')
    Principal = apps.get_model('iam', 'Principal')
    Resource = apps.get_model('iam', 'Resource')
    Grant = apps.get_model('iam', 'Grant')

    personal_tenants = {}
    for user in User.objects.all().iterator():
        tenant, _ = Tenant.objects.get_or_create(
            type='personal',
            external_id=f'personal:{user.uid}',
            defaults={'name': '个人空间', 'owner_user_id': user.id},
        )
        if tenant.owner_user_id != user.id:
            tenant.owner_user_id = user.id
            tenant.save(update_fields=['owner_user'])
        TenantMembership.objects.update_or_create(
            tenant_id=tenant.id,
            user_id=user.id,
            defaults={'role': 'super_admin', 'status': 'active'},
        )
        personal_tenants[user.id] = tenant

    organization_tenants = {}
    companies = Company.objects.filter(verification_status='verified')
    for company in companies.iterator():
        company_resource = Resource.objects.filter(
            type='company', external_id=company.cid,
        ).first()
        super_uid = None
        if company_resource:
            super_uid = Grant.objects.filter(
                resource_id=company_resource.id,
                permissions=4,
                principal__type='user',
            ).order_by('created_at', 'id').values_list('principal__external_id', flat=True).first()
        verification = CompanyVerification.objects.filter(
            id=company.verified_verification_id,
            applicant_user__isnull=False,
        ).first() if company.verified_verification_id else None
        super_user = verification.applicant_user if verification else None
        if super_user is None:
            super_user = User.objects.filter(uid=super_uid).first() if super_uid else None
        if super_user is None:
            super_user = company.account
        tenant, _ = Tenant.objects.get_or_create(
            type='organization',
            external_id=f'organization:{company.cid}',
            defaults={
                'name': company.company_name,
                'super_admin_user_id': super_user.id if super_user else None,
            },
        )
        changed = []
        if not tenant.name:
            tenant.name = company.company_name
            changed.append('name')
        if tenant.super_admin_user_id is None and super_user:
            tenant.super_admin_user_id = super_user.id
            changed.append('super_admin_user')
        if changed:
            tenant.save(update_fields=changed)
        organization_tenants[company.id] = tenant

        if super_user:
            TenantMembership.objects.update_or_create(
                tenant_id=tenant.id,
                user_id=super_user.id,
                defaults={'role': 'super_admin', 'status': 'active'},
            )
        for member in CompanyMember.objects.filter(
            company_id=company.id,
            status='active',
            user__isnull=False,
        ).iterator():
            TenantMembership.objects.update_or_create(
                tenant_id=tenant.id,
                user_id=member.user_id,
                defaults={
                    'role': 'super_admin' if member.user_id == tenant.super_admin_user_id else 'member',
                    'status': 'active',
                },
            )

        company_resource, _ = Resource.objects.get_or_create(
            type='company',
            external_id=company.cid,
            defaults={'tenant_id': tenant.id, 'creator_id': company.account_id},
        )
        company_resource.tenant_id = tenant.id
        if company_resource.creator_id is None:
            company_resource.creator_id = company.account_id
        company_resource.save(update_fields=['tenant', 'creator'])

    bound_gate_ids = set()
    for binding in CompanyGate.objects.select_related('company', 'opengate', 'opengate__owner').iterator():
        tenant = organization_tenants.get(binding.company_id)
        if tenant is None:
            continue
        gate = binding.opengate
        bound_gate_ids.add(gate.id)
        project_resource, _ = Resource.objects.get_or_create(
            type='project',
            external_id=gate.gid,
            defaults={'tenant_id': tenant.id, 'creator_id': gate.owner_id},
        )
        project_resource.tenant_id = tenant.id
        if project_resource.creator_id is None:
            project_resource.creator_id = gate.owner_id
        project_resource.save(update_fields=['tenant', 'creator'])
        for table in GatesTable.objects.filter(gate_id=gate.id).iterator():
            table_resource, _ = Resource.objects.get_or_create(
                type='table',
                external_id=table.tid,
                defaults={
                    'tenant_id': tenant.id,
                    'creator_id': gate.owner_id,
                    'parent_id': project_resource.id,
                },
            )
            table_resource.tenant_id = tenant.id
            table_resource.parent_id = project_resource.id
            if table_resource.creator_id is None:
                table_resource.creator_id = gate.owner_id
            table_resource.save(update_fields=['tenant', 'parent', 'creator'])

    for gate in Gates.objects.exclude(id__in=bound_gate_ids).select_related('owner').iterator():
        tenant = personal_tenants.get(gate.owner_id)
        if tenant is None:
            continue
        project_resource, _ = Resource.objects.get_or_create(
            type='project',
            external_id=gate.gid,
            defaults={'tenant_id': tenant.id, 'creator_id': gate.owner_id},
        )
        project_resource.tenant_id = tenant.id
        if project_resource.creator_id is None:
            project_resource.creator_id = gate.owner_id
        project_resource.save(update_fields=['tenant', 'creator'])
        for table in GatesTable.objects.filter(gate_id=gate.id).iterator():
            table_resource, _ = Resource.objects.get_or_create(
                type='table',
                external_id=table.tid,
                defaults={
                    'tenant_id': tenant.id,
                    'creator_id': gate.owner_id,
                    'parent_id': project_resource.id,
                },
            )
            table_resource.tenant_id = tenant.id
            table_resource.parent_id = project_resource.id
            if table_resource.creator_id is None:
                table_resource.creator_id = gate.owner_id
            table_resource.save(update_fields=['tenant', 'parent', 'creator'])

    for transparent in TransparentCompany.objects.select_related('company').iterator():
        tenant = organization_tenants.get(transparent.company_id)
        if tenant is None:
            continue
        knowledge_resource, _ = Resource.objects.get_or_create(
            type='knowledge_base',
            external_id=f'knowledge:{transparent.company.cid}',
            defaults={
                'tenant_id': tenant.id,
                'creator_id': transparent.company.account_id,
            },
        )
        for agent in Agent.objects.filter(transparentCompany_id=transparent.id).exclude(status='deprecated').iterator():
            Resource.objects.get_or_create(
                type='agent',
                external_id=f'agent:{transparent.company.cid}:{agent.agentId}',
                defaults={
                    'tenant_id': tenant.id,
                    'creator_id': transparent.company.account_id,
                },
            )

        for scope in CompanyScopedAdmin.objects.filter(
            company_id=transparent.company_id,
            active=True,
        ).exclude(user__isnull=True).iterator():
            TenantMembership.objects.update_or_create(
                tenant_id=tenant.id,
                user_id=scope.user_id,
                defaults={'role': 'member', 'status': 'active'},
            )
            principal, _ = Principal.objects.get_or_create(type='user', external_id=scope.user.uid)
            for agent_id in scope.agent_ids or []:
                resource = Resource.objects.filter(
                    type='agent',
                    external_id=f'agent:{transparent.company.cid}:{agent_id}',
                ).first()
                if resource:
                    Grant.objects.update_or_create(
                        principal_id=principal.id,
                        resource_id=resource.id,
                        defaults={'permissions': 2, 'inherit': False, 'granter_id': scope.granted_by_id},
                    )
            knowledge_role = 0
            for rule in scope.file_rules or []:
                actions = rule.get('actions', []) if isinstance(rule, dict) else []
                if 'delete' in actions:
                    knowledge_role = max(knowledge_role, 3)
                elif 'write' in actions:
                    knowledge_role = max(knowledge_role, 2)
                elif 'read' in actions:
                    knowledge_role = max(knowledge_role, 1)
            if knowledge_role:
                Grant.objects.update_or_create(
                    principal_id=principal.id,
                    resource_id=knowledge_resource.id,
                    defaults={
                        'permissions': knowledge_role,
                        'inherit': False,
                        'granter_id': scope.granted_by_id,
                    },
                )

    # 历史直授用户必须成为对应 Tenant 的成员，否则 Tenant 默认拒绝会改变现有行为。
    for grant in Grant.objects.filter(
        resource__tenant__isnull=False,
        principal__type='user',
    ).select_related('resource__tenant', 'principal').iterator():
        user = User.objects.filter(uid=grant.principal.external_id).first()
        if user is None:
            continue
        role = (
            'super_admin'
            if grant.resource.tenant.super_admin_user_id == user.id
            else 'member'
        )
        TenantMembership.objects.update_or_create(
            tenant_id=grant.resource.tenant_id,
            user_id=user.id,
            defaults={'role': role, 'status': 'active'},
        )

    # 企业成员停用必须覆盖历史 Grant 的兼容性补录，避免离职员工继续生效。
    for member in CompanyMember.objects.exclude(status='active').exclude(user__isnull=True).iterator():
        tenant = organization_tenants.get(member.company_id)
        if tenant and member.user_id != tenant.super_admin_user_id:
            TenantMembership.objects.filter(
                tenant_id=tenant.id,
                user_id=member.user_id,
            ).update(status='disabled')


class Migration(migrations.Migration):

    dependencies = [
        ('iam', '0002_group_groupmembership_permissionauditlog_and_more'),
        ('company', '0041_seed_default_transparent_company_skills'),
        ('opengate', '0002_apikey_audit_fields'),
        ('user', '0003_user_public_id'),
    ]

    operations = [
        migrations.RunPython(backfill_unified_permissions, migrations.RunPython.noop),
    ]
