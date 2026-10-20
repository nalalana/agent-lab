import json

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.company.models import Company, CompanyMember, companyGate
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource, Tenant, TenantMembership
from apps.opengate.models import Gates, GatesTable
from apps.opengate.services import OpenGateService
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ProjectDashboardPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(uid="dashboard-owner", username="Owner", created_at="1", updated_at="1")
        self.employee = User.objects.create(uid="dashboard-employee", username="Employee", created_at="1", updated_at="1")
        self.outsider = User.objects.create(uid="dashboard-outsider", username="Outsider", created_at="1", updated_at="1")
        self.company = Company.objects.create(
            cid="dashboard-company", company_name="Dashboard Company", account=self.owner,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
        )
        self.member = CompanyMember.objects.create(
            company=self.company, user=self.employee, memberUid=self.employee.uid,
            name=self.employee.username, status=CompanyMember.STATUS_ACTIVE,
            created_at="1", updated_at="1",
        )
        self.tenant = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION, external_id=f"organization:{self.company.cid}",
            name=self.company.company_name, super_admin_user=self.owner,
        )
        TenantMembership.objects.create(tenant=self.tenant, user=self.owner, role=TenantMembership.ROLE_SUPER_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.employee)
        self.owner_principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.owner.uid)
        self.employee_principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.employee.uid)
        self.company_resource = Resource.objects.create(type=Resource.TYPE_COMPANY, external_id=self.company.cid, tenant=self.tenant)
        self.gate = Gates.objects.create(
            gid="dashboard-project", gatename="Company Project", description="Basic description",
            label=Gates.LABEL_COMPANY, owner=self.owner, created_at="1", updated_at="1",
        )
        companyGate.objects.create(company=self.company, opengate=self.gate)
        self.project = Resource.objects.create(type=Resource.TYPE_PROJECT, external_id=self.gate.gid, tenant=self.tenant)
        self.table = GatesTable.objects.create(
            tid="dashboard-table", gate=self.gate, name="Secret table",
            created_at="1", updated_at="1",
        )
        self.table_resource = Resource.objects.create(
            type=Resource.TYPE_TABLE, external_id=self.table.tid, parent=self.project, tenant=self.tenant,
        )

    def headers(self, user):
        return {"HTTP_ACCESS_TOKEN": sign_token({"user_id": user.id})}

    def detail(self, user):
        return self.client.post(
            "/api/opengate/gates/details", json.dumps({"gid": self.gate.gid}),
            content_type="application/json", **self.headers(user),
        )

    def test_ungranted_employee_gets_only_basic_information_and_no_business_permissions(self):
        grant_count = Grant.objects.count()
        response = self.detail(self.employee)
        self.assertEqual(response.status_code, 200)
        detail = response.json()["data"]
        self.assertEqual(detail["gatename"], self.gate.gatename)
        self.assertEqual(detail["dashboard_access"], {"can_view_info": True, "permissions": 0})
        self.assertFalse(detail["can_manage_public_access"])
        self.assertNotIn("owner", detail)
        self.assertNotIn("public_access", detail)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(Grant.objects.count(), grant_count)
        tables = self.client.get("/api/opengate/gates/tables", {"gid": self.gate.gid}, **self.headers(self.employee))
        self.assertEqual(tables.status_code, 404)
        self.assertFalse(OpenGateService.can_read_table(self.employee, self.table))
        self.assertFalse(OpenGateService.can_write_table(self.employee, self.table))
        self.assertFalse(OpenGateService.can_manage_project(self.employee, self.gate))

    def test_private_company_metadata_is_not_disclosed_to_outsiders_or_disabled_employees(self):
        self.assertEqual(self.detail(self.outsider).status_code, 404)
        self.member.status = CompanyMember.STATUS_DISABLED
        self.member.save(update_fields=["status"])
        self.assertEqual(self.detail(self.employee).status_code, 404)

    def test_company_suspension_blocks_info_only_employee_access(self):
        self.company.service_suspended_at = timezone.now()
        self.company.service_suspension_reason = "test"
        self.company.save(update_fields=["service_suspended_at", "service_suspension_reason"])
        self.assertEqual(self.detail(self.employee).status_code, 404)

    def test_each_project_role_and_revocation_are_reflected_in_navigation_metadata(self):
        for permission in (1, 2, 3, 4):
            Grant.objects.update_or_create(
                principal=self.employee_principal, resource=self.project,
                defaults={"permissions": permission},
            )
            response = self.detail(self.employee)
            self.assertEqual(response.status_code, 200)
            detail = response.json()["data"]
            self.assertEqual(detail["dashboard_access"]["permissions"], permission)
            self.assertEqual(detail["can_manage_public_access"], permission >= 3)
        Grant.objects.filter(principal=self.employee_principal, resource=self.project).delete()
        self.assertEqual(self.detail(self.employee).json()["data"]["dashboard_access"]["permissions"], 0)

    def test_table_authorization_management_requires_manager_not_editor(self):
        Grant.objects.create(principal=self.employee_principal, resource=self.project, permissions=Grant.ROLE_EDITOR)
        self.assertFalse(OpenGateService.list_table_members(self.employee, self.table.tid, 20, 0)[0])
        self.assertFalse(OpenGateService.update_table_member(self.employee, self.table.tid, self.outsider.uid, 1)[0])
        self.assertFalse(OpenGateService.remove_table_member(self.employee, self.table.tid, self.outsider.uid)[0])
        self.assertTrue(OpenGateService.list_table_members(self.owner, self.table.tid, 20, 0)[0])

    def test_independent_table_permissions_do_not_inherit_project_management(self):
        Grant.objects.create(principal=self.employee_principal, resource=self.project, permissions=Grant.ROLE_MANAGER)
        self.table_resource.permission_mode = Resource.PERMISSION_MODE_INDEPENDENT
        self.table_resource.save(update_fields=["permission_mode"])
        response = self.client.post(
            "/api/iam/check-access",
            json.dumps({"resource_type": "table", "resource_external_id": self.table.tid, "permission": "read"}),
            content_type="application/json", **self.headers(self.employee),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["data"]["allowed"])

    @patch("apps.opengate.services._moderate_opengate_texts", return_value=(True, "ok"))
    def test_project_editor_can_grow_and_shrink_inherited_table_columns(self, _moderation):
        Grant.objects.create(principal=self.employee_principal, resource=self.project, permissions=Grant.ROLE_EDITOR)
        for count in (5, 2):
            response = self.client.post(
                "/api/opengate/tables/update",
                json.dumps({"tid": self.table.tid, "tableName": self.table.name,
                            "columnTitles": [f"列{index}" for index in range(count)],
                            "numColumns": count, "permission": 0}),
                content_type="application/json", **self.headers(self.employee),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"], 1)
            self.table.refresh_from_db()
            self.assertEqual(self.table.num_columns, count)
            self.assertEqual(len(self.table.columns_config), count)
        details = OpenGateService.get_table_full_details(self.employee, self.table.tid)[1]
        self.assertTrue(details["can_edit_schema"])

    @patch("apps.opengate.services._moderate_opengate_texts", return_value=(True, "ok"))
    def test_contributor_cannot_edit_schema_or_edit_an_independent_table(self, _moderation):
        grant = Grant.objects.create(principal=self.employee_principal, resource=self.project, permissions=Grant.ROLE_CONTRIBUTOR)
        self.assertFalse(OpenGateService.update_table_full(self.employee, self.table.tid, self.table.name, column_titles=["A"], num_columns=1)[0])
        grant.permissions = Grant.ROLE_EDITOR
        grant.save(update_fields=["permissions"])
        self.table_resource.permission_mode = Resource.PERMISSION_MODE_INDEPENDENT
        self.table_resource.save(update_fields=["permission_mode"])
        self.assertFalse(OpenGateService.update_table_full(self.employee, self.table.tid, self.table.name, column_titles=["A"], num_columns=1)[0])

    @patch("apps.opengate.services._moderate_opengate_texts", return_value=(True, "ok"))
    def test_project_public_scope_caps_table_creation_and_updates(self, _moderation):
        for policy, maximum in (("internal", 0), ("public_read", 1), ("public_write", 2)):
            self.gate.public_access = policy
            self.gate.save(update_fields=["public_access"])
            for permission in (0, 1, 2):
                ok, _message = OpenGateService.update_table_full(self.owner, self.table.tid, self.table.name, permission=permission)
                self.assertEqual(ok, permission <= maximum)
                if permission > maximum:
                    with patch("apps.opengate.services.ImmudbService") as immudb:
                        self.assertFalse(OpenGateService.create_table_full(self.owner, self.gate.gid, "new", "", permission, ["A"], 1)[0])
                        immudb.assert_not_called()

    def test_tightening_project_scope_clamps_historical_tables(self):
        self.gate.public_access = Gates.PUBLIC_ACCESS_WRITE
        self.gate.save(update_fields=["public_access"])
        self.table.permission = GatesTable.PERM_WRITE
        self.table.save(update_fields=["permission"])
        self.assertTrue(OpenGateService.update_public_access(self.owner, self.gate, Gates.PUBLIC_ACCESS_READ)[0])
        self.table.refresh_from_db()
        self.assertEqual(self.table.permission, GatesTable.PERM_READ)
        self.assertTrue(OpenGateService.update_public_access(self.owner, self.gate, Gates.PUBLIC_ACCESS_INTERNAL)[0])
        self.table.refresh_from_db()
        self.assertEqual(self.table.permission, GatesTable.PERM_PRIVATE)

    def test_historical_public_table_cannot_escape_an_internal_project(self):
        self.table.permission = GatesTable.PERM_WRITE
        self.table.save(update_fields=["permission"])
        self.assertFalse(OpenGateService.can_read_table(None, self.table))
        self.assertFalse(OpenGateService.can_write_table(self.outsider, self.table))
        self.assertFalse(OpenGateService._can_read_table(None, self.table))
        self.assertFalse(OpenGateService._can_read_table(self.outsider, self.table))

    def test_company_member_administration_requires_management_level(self):
        Grant.objects.create(principal=self.employee_principal, resource=self.company_resource, permissions=Grant.ROLE_EDITOR)
        response = self.client.get("/api/company/admin-access/me", {"cid": self.company.cid}, **self.headers(self.employee))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["permissions"], 3)
        self.assertFalse(response.json()["data"]["can_manage_members"])
        self.assertFalse(CompanyService.list_company_members(self.company.cid, self.employee)[0])
        self.assertFalse(CompanyService.add_company_member_by_public_id(self.company.cid, self.employee, self.outsider.public_id)[0])
        self.assertTrue(CompanyService.list_company_members(self.company.cid, self.owner)[0])
