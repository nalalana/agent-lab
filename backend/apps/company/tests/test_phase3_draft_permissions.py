import json
import time
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from apps.company.models import Company, CompanyMember, TransparentCompany
from apps.company.permissions import can_access_company_draft
from apps.company.services import CompanyService
from apps.company.views import company_list_by_user
from apps.follow.models import FollowingCompany
from apps.follow.services import FollowingService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import Gates
from apps.ops.models import CompanyFollow, PopularCompany
from apps.ops.services import OpsService
from apps.user.models import User
from common.utils.Login_utils import sign_token


VALID_REGISTRATION_NO = '91350211M000100Y46'


class Phase3DraftPermissionTests(TestCase):
    def setUp(self):
        self.now = str(time.time())[:20]
        self.creator = self._user('phase3-creator', 'creator@example.com')
        self.other = self._user('phase3-other', 'other@example.com')

    def _user(self, uid, email):
        return User.objects.create(
            uid=uid,
            username=uid,
            email=email,
            created_at=self.now,
            updated_at=self.now,
        )

    def _company(self, cid, *, account=None, verified=False, registration_no=''):
        company = Company.objects.create(
            account=account,
            cid=cid,
            company_name=cid,
            registration_no=registration_no,
            verification_status=(
                Company.VERIFICATION_STATUS_VERIFIED
                if verified
                else Company.VERIFICATION_STATUS_UNVERIFIED
            ),
            created_at=self.now,
            updated_at=self.now,
        )
        get_or_create_resource(Resource.TYPE_COMPANY, cid)
        return company

    @patch('apps.company.services.ImageModerationService.require_approved_public_image', return_value=(True, '', ''))
    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    def test_create_company_builds_unprivileged_draft(self, _moderate, _image):
        ok, company = CompanyService.create_company(
            company_name='Phase 3 draft',
            owner=self.creator,
            registration_no=VALID_REGISTRATION_NO,
        )

        self.assertTrue(ok)
        self.assertEqual(company.account, self.creator)
        self.assertEqual(company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=company.cid)
        self.assertFalse(
            Principal.objects.filter(type=Principal.TYPE_USER, external_id=self.creator.uid).exists()
        )
        self.assertFalse(Grant.objects.filter(resource=resource).exists())
        self.assertFalse(CompanyMember.objects.filter(company=company, role='owner').exists())

    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    def test_creator_can_read_and_update_only_their_unverified_draft(self, _moderate):
        draft = self._company('phase3-own-draft', account=self.creator)

        self.assertTrue(can_access_company_draft(draft, self.creator))
        self.assertFalse(can_access_company_draft(draft, self.other))
        detail_ok, detail = CompanyService.get_company_detail(draft.cid, user=self.creator)
        hidden_ok, hidden_message = CompanyService.get_company_detail(draft.cid, user=self.other)
        update_ok, _ = CompanyService.update_company(
            cid=draft.cid,
            user=self.creator,
            description='Creator-owned draft',
        )
        other_ok, other_message = CompanyService.update_company(
            cid=draft.cid,
            user=self.other,
            description='Unauthorized edit',
        )
        status_ok, status_message = CompanyService.update_company(
            cid=draft.cid,
            user=self.creator,
            status=Company.COMPANY_STATUS_TRANSPARENT,
        )

        self.assertTrue(detail_ok)
        self.assertEqual(detail['verification_status'], Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertNotIn('proxy_organization_open_id', detail)
        self.assertFalse(hidden_ok)
        self.assertEqual(hidden_message, '公司不存在')
        self.assertTrue(update_ok)
        self.assertFalse(other_ok)
        self.assertEqual(other_message, '权限不足')
        self.assertFalse(status_ok)
        self.assertEqual(status_message, '未认证企业草稿不能修改企业状态')
        draft.refresh_from_db()
        self.assertEqual(draft.description, 'Creator-owned draft')
        self.assertEqual(draft.status, None)

    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    def test_verified_created_by_has_no_rights_but_iam_admin_can_update(self, _moderate):
        company = self._company(
            'phase3-verified',
            account=self.creator,
            verified=True,
            registration_no=VALID_REGISTRATION_NO,
        )

        creator_ok, creator_message = CompanyService.update_company(
            cid=company.cid,
            user=self.creator,
            description='Created-by is not authority',
        )
        admin_principal = get_or_create_principal(Principal.TYPE_USER, self.other.uid)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=company.cid)
        grant_permissions(admin_principal, resource, Grant.PERM_ADMIN)
        admin_ok, _ = CompanyService.update_company(
            cid=company.cid,
            user=self.other,
            company_name=company.company_name,
            registration_no='91350211-m000100y46',
            description='IAM-authorized update',
        )
        locked_ok, locked_message = CompanyService.update_company(
            cid=company.cid,
            user=self.other,
            company_name='Changed legal entity',
        )

        self.assertFalse(creator_ok)
        self.assertEqual(creator_message, '权限不足')
        self.assertTrue(admin_ok)
        self.assertFalse(locked_ok)
        self.assertEqual(locked_message, '已认证企业主体信息不可修改')
        company.refresh_from_db()
        self.assertEqual(company.company_name, 'phase3-verified')
        self.assertEqual(company.description, 'IAM-authorized update')

    def test_my_companies_combines_own_drafts_and_iam_without_created_by_leak(self):
        own_draft = self._company('phase3-own-list', account=self.creator)
        verified_created = self._company(
            'phase3-created-but-verified', account=self.creator, verified=True
        )
        iam_company = self._company('phase3-iam-company', account=self.other, verified=True)
        principal = get_or_create_principal(Principal.TYPE_USER, self.creator.uid)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=iam_company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN)

        ok, payload = CompanyService.list_companies_by_user(
            user=self.creator,
            limit=20,
            offset=0,
        )

        self.assertTrue(ok)
        cids = {item['cid'] for item in payload['items']}
        self.assertEqual(cids, {own_draft.cid, iam_company.cid})
        self.assertNotIn(verified_created.cid, cids)

    def test_verified_admin_can_unbind_without_deleting_company(self):
        company = self._company(
            'phase3-unbind-verified',
            account=self.creator,
            verified=True,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.creator.uid)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN)
        member = CompanyMember.objects.create(
            company=company,
            user=self.creator,
            name=self.creator.username,
            email=self.creator.email,
            status=CompanyMember.STATUS_ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )

        ok, message = CompanyService.unbind_company(company.cid, self.creator)

        self.assertTrue(ok)
        self.assertIsNone(message)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        self.assertFalse(Grant.objects.filter(principal=principal, resource=resource).exists())
        self.assertFalse(CompanyMember.objects.filter(pk=member.pk).exists())

    def test_company_unbind_requires_verified_admin_grant(self):
        draft = self._company('phase3-unbind-draft', account=self.creator)
        verified = self._company('phase3-unbind-no-grant', verified=True)

        draft_ok, draft_message = CompanyService.unbind_company(draft.cid, self.creator)
        verified_ok, verified_message = CompanyService.unbind_company(
            verified.cid,
            self.creator,
        )

        self.assertFalse(draft_ok)
        self.assertEqual(draft_message, '未完成认证的企业请使用删除企业操作')
        self.assertFalse(verified_ok)
        self.assertEqual(verified_message, '权限不足')

    def test_list_by_user_ignores_spoofed_uid(self):
        own_draft = self._company('phase3-view-own', account=self.creator)
        other_draft = self._company('phase3-view-other', account=self.other)
        request = RequestFactory().post(
            '/api/company/list-by-user',
            data=json.dumps({'uid': self.other.uid, 'limit': 20, 'offset': 0}),
            content_type='application/json',
            HTTP_ACCESS_TOKEN=sign_token({'user_id': self.creator.id}),
        )

        response = company_list_by_user(request)
        payload = json.loads(response.content)
        cids = {item['cid'] for item in payload['data'][1]['items']}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cids, {own_draft.cid})
        self.assertNotIn(other_draft.cid, cids)

    def test_draft_creator_cannot_use_formal_company_capabilities(self):
        draft = self._company('phase3-formal-denied', account=self.creator)
        gate = Gates.objects.create(
            gid='phase3-gate',
            owner=self.creator,
            gatename='Phase 3 Gate',
            created_at=self.now,
            updated_at=self.now,
        )

        bind_ok, bind_message = CompanyService.bind_program(draft.cid, gate.gid, self.creator)
        member_ok, member_message = CompanyService.list_company_members(
            cid=draft.cid,
            user=self.creator,
        )
        upgrade_ok, upgrade_message = CompanyService.upgrade_to_transparent_company(
            cid=draft.cid,
            user=self.creator,
        )

        self.assertFalse(bind_ok)
        self.assertEqual(bind_message, '权限不足')
        self.assertFalse(member_ok)
        self.assertEqual(member_message, '权限不足')
        self.assertFalse(upgrade_ok)
        self.assertEqual(upgrade_message, '权限不足')
        self.assertFalse(TransparentCompany.objects.filter(company=draft).exists())
        self.assertFalse(Grant.objects.filter(resource__external_id=draft.cid).exists())

    def test_draft_delete_removes_only_empty_creator_owned_draft(self):
        draft = self._company('phase3-delete-empty', account=self.creator)
        resource_id = Resource.objects.get(external_id=draft.cid).id

        ok, message = CompanyService.delete_company_draft(draft.cid, self.creator)

        self.assertTrue(ok)
        self.assertIsNone(message)
        self.assertFalse(Company.objects.filter(pk=draft.pk).exists())
        self.assertFalse(Resource.objects.filter(pk=resource_id).exists())

    def test_draft_delete_rejects_wrong_user_verified_and_formal_state(self):
        wrong_user_draft = self._company('phase3-delete-wrong-user', account=self.creator)
        verified = self._company('phase3-delete-verified', account=self.creator, verified=True)
        with_grant = self._company('phase3-delete-grant', account=self.creator)
        with_member = self._company('phase3-delete-member', account=self.creator)
        with_child = self._company('phase3-delete-child', account=self.creator)
        principal = get_or_create_principal(Principal.TYPE_USER, self.other.uid)
        grant_permissions(
            principal,
            Resource.objects.get(external_id=with_grant.cid),
            Grant.PERM_READ,
        )
        CompanyMember.objects.create(
            company=with_member,
            user=self.other,
            name=self.other.username,
            email=self.other.email,
            created_at=self.now,
            updated_at=self.now,
        )
        get_or_create_resource(
            Resource.TYPE_PROJECT,
            'phase3-child-resource',
            parent=Resource.objects.get(external_id=with_child.cid),
        )

        cases = [
            (wrong_user_draft, self.other, '权限不足'),
            (verified, self.creator, '权限不足'),
            (with_grant, self.creator, '草稿已有关联的正式数据，无法删除'),
            (with_member, self.creator, '草稿已有关联的正式数据，无法删除'),
            (with_child, self.creator, '草稿已有关联的正式数据，无法删除'),
        ]
        for company, operator, expected in cases:
            with self.subTest(cid=company.cid):
                ok, message = CompanyService.delete_company_draft(company.cid, operator)
                self.assertFalse(ok)
                self.assertEqual(message, expected)
                self.assertTrue(Company.objects.filter(pk=company.pk).exists())


class Phase3PublicVisibilityTests(TestCase):
    def setUp(self):
        self.now = str(time.time())[:20]
        self.user = User.objects.create(
            uid='phase3-public-user',
            username='phase3-public-user',
            email='public@example.com',
            created_at=self.now,
            updated_at=self.now,
        )
        self.verified = self._company('phase3-public-verified', verified=True)
        self.draft = self._company('phase3-public-draft', verified=False)

    def _company(self, cid, *, verified):
        return Company.objects.create(
            account=self.user,
            cid=cid,
            company_name=cid,
            verification_status=(
                Company.VERIFICATION_STATUS_VERIFIED
                if verified
                else Company.VERIFICATION_STATUS_UNVERIFIED
            ),
            followers=0,
            created_at=self.now,
            updated_at=self.now,
        )

    def test_public_detail_search_and_transparent_probe_hide_drafts(self):
        public_detail_ok, _ = CompanyService.get_company_detail(self.verified.cid)
        draft_detail_ok, _ = CompanyService.get_company_detail(self.draft.cid)
        search = CompanyService.search_companies(keyword='phase3-public', offset=0, limit=20)
        transparent_ok, transparent_message = CompanyService.is_transparent_company(self.draft.cid)

        self.assertTrue(public_detail_ok)
        self.assertFalse(draft_detail_ok)
        self.assertEqual([item['cid'] for item in search['items']], [self.verified.cid])
        self.assertFalse(transparent_ok)
        self.assertEqual(transparent_message, '公司不存在')

    def test_following_rejects_and_filters_unverified_companies(self):
        follow_draft_ok, follow_draft_message = FollowingService.follow_company(
            self.user,
            self.draft.cid,
        )
        follow_verified_ok, _ = FollowingService.follow_company(self.user, self.verified.cid)
        FollowingCompany.objects.create(
            user=self.user,
            following_company=self.draft,
            updated_at=self.now,
        )
        list_ok, payload = FollowingService.get_following_companies(self.user)

        self.assertFalse(follow_draft_ok)
        self.assertEqual(follow_draft_message, '公司不存在')
        self.assertTrue(follow_verified_ok)
        self.assertTrue(list_ok)
        self.assertEqual(payload['total'], 1)
        self.assertEqual([item['cid'] for item in payload['companies']], [self.verified.cid])

    def test_hot_and_popular_company_lists_include_only_verified(self):
        CompanyFollow.objects.create(user=self.user, target=self.verified, created_at=time.time())
        CompanyFollow.objects.create(user=self.user, target=self.draft, created_at=time.time())
        PopularCompany.objects.create(company=self.verified)
        PopularCompany.objects.create(company=self.draft)

        hot = OpsService.list_hot_company()
        popular = OpsService.get_popular_company()
        set_draft_ok = OpsService.set_popular_company([self.draft.id])

        self.assertEqual([item['company_id'] for item in hot], [self.verified.id])
        self.assertEqual([item['company_id'] for item in popular], [self.verified.id])
        self.assertFalse(set_draft_ok)
