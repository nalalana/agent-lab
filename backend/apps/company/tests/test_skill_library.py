from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.company.default_skills_v1 import (
    DEFAULT_SKILL_IDS_V1,
    DefaultSkillConflict,
    ensure_default_skills_v1,
)
from apps.company.models import (
    Company,
    TransparentCompanyAgentSkillBinding,
    TransparentCompany,
    TransparentCompanySkill,
)
from apps.company.serializers import TransparentCompanySkillUpsertSerializer
from apps.company.services import CompanyService


class SkillLibraryTests(TestCase):
    def setUp(self):
        fixture = create_memory_fixture("skill-library")
        self.company = fixture["company_a"]
        self.owner = fixture["owner"]
        self.agent = fixture["agent_a1"]
        self.transparent_company = self.agent.transparentCompany

    def test_default_skills_seed_all_five_and_are_idempotent(self):
        self.assertEqual(
            ensure_default_skills_v1(self.transparent_company, TransparentCompanySkill),
            5,
        )
        self.assertEqual(
            ensure_default_skills_v1(self.transparent_company, TransparentCompanySkill),
            0,
        )
        skills = list(TransparentCompanySkill.objects.filter(
            transparentCompany=self.transparent_company,
        ).order_by("priority"))
        self.assertEqual([skill.skillId for skill in skills], list(DEFAULT_SKILL_IDS_V1))
        self.assertTrue(all(
            skill.isSystemSkill and skill.sourceType == "system"
            and skill.enabled and skill.status == "active"
            and "## 指令" in skill.content
            for skill in skills
        ))

    def test_seeding_preserves_existing_status_content_and_custom_skills(self):
        ensure_default_skills_v1(self.transparent_company, TransparentCompanySkill)
        system_skill = TransparentCompanySkill.objects.get(
            transparentCompany=self.transparent_company,
            skillId=DEFAULT_SKILL_IDS_V1[0],
        )
        system_skill.enabled = False
        system_skill.status = TransparentCompanySkill.STATUS_INACTIVE
        system_skill.content = "locally configured content"
        system_skill.save(update_fields=["enabled", "status", "content"])
        custom = self._create_skill_record("custom-skill")

        self.assertEqual(
            ensure_default_skills_v1(self.transparent_company, TransparentCompanySkill),
            0,
        )
        system_skill.refresh_from_db()
        custom.refresh_from_db()
        self.assertFalse(system_skill.enabled)
        self.assertEqual(system_skill.status, TransparentCompanySkill.STATUS_INACTIVE)
        self.assertEqual(system_skill.content, "locally configured content")
        self.assertEqual(custom.skillId, "custom-skill")
        self.assertEqual(TransparentCompanySkill.objects.filter(
            transparentCompany=self.transparent_company,
        ).count(), 6)

    def test_seeding_rejects_reserved_id_collision_without_replacing_custom_skill(self):
        custom = self._create_skill_record(DEFAULT_SKILL_IDS_V1[0])
        with self.assertRaises(DefaultSkillConflict):
            ensure_default_skills_v1(self.transparent_company, TransparentCompanySkill)
        custom.refresh_from_db()
        self.assertFalse(custom.isSystemSkill)
        self.assertEqual(custom.sourceType, TransparentCompanySkill.SOURCE_MANUAL)

    def test_upgrade_backfills_existing_transparent_company(self):
        with (
            patch("apps.company.services.ImageModerationService.require_approved_public_image", return_value=(True, "", "")),
            patch("apps.company.services._moderate_company_texts", return_value=(True, "")),
            patch.object(CompanyService, "ensure_default_transparent_agent", return_value=self.agent),
            patch.object(CompanyService, "_ensure_company_viking_binding", return_value=SimpleNamespace(status="active")),
            patch.object(CompanyService, "_enqueue_viking_provisioning"),
        ):
            ok, _ = CompanyService.upgrade_to_transparent_company(
                cid=self.company.cid,
                philosophy="hello",
                user=self.owner,
            )

        self.assertTrue(ok)
        self.assertEqual(TransparentCompanySkill.objects.filter(
            transparentCompany=self.transparent_company,
            isSystemSkill=True,
        ).count(), 5)

    def test_new_transparent_company_gets_five_default_skills(self):
        company = Company.objects.create(
            account=self.owner,
            cid="new-default-skills-company",
            company_name="New Default Skills Company",
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            status=Company.COMPANY_STATUS_COMMON,
            created_at="1",
            updated_at="1",
        )
        with (
            patch("apps.company.services.can_access_formal_company", return_value=True),
            patch("apps.company.services.ImageModerationService.require_approved_public_image", return_value=(True, "", "")),
            patch("apps.company.services._moderate_company_texts", return_value=(True, "")),
            patch.object(CompanyService, "ensure_default_transparent_agent", return_value=SimpleNamespace(pk=1)),
            patch.object(CompanyService, "_ensure_company_viking_binding", return_value=SimpleNamespace(status="active")),
            patch.object(CompanyService, "_enqueue_viking_provisioning"),
        ):
            ok, _ = CompanyService.create_transparent_company(
                cid=company.cid,
                avatar="",
                prompts="hello",
                user=self.owner,
            )

        self.assertTrue(ok)
        self.assertEqual(TransparentCompany.objects.filter(company=company).count(), 1)
        self.assertEqual(TransparentCompanySkill.objects.filter(
            transparentCompany__company=company,
            isSystemSkill=True,
        ).count(), 5)

    def _create_skill(self, name: str, skill_id: str | None = None):
        return CompanyService.upsert_transparent_company_skill(
            cid=self.company.cid,
            user=self.owner,
            name=name,
            skill_id=skill_id,
            category="general",
            enabled=True,
            short_description=f"{name} description",
            status=TransparentCompanySkill.STATUS_ACTIVE,
            source_type=TransparentCompanySkill.SOURCE_MANUAL,
            usage=f"Use {name}",
            instructions=f"Run {name} safely",
        )

    def _create_skill_record(self, skill_id: str, *, system: bool = False):
        return TransparentCompanySkill.objects.create(
            transparentCompany=self.transparent_company,
            skillId=skill_id,
            name=skill_id,
            category='通用',
            enabled=True,
            status=TransparentCompanySkill.STATUS_ACTIVE,
            sourceType=(
                TransparentCompanySkill.SOURCE_SYSTEM
                if system
                else TransparentCompanySkill.SOURCE_MANUAL
            ),
            runtimeStatus=TransparentCompanySkill.RUNTIME_STATUS_ACTIVE,
            isSystemSkill=system,
            updated_at='1',
        )

    def test_supported_category_options_and_default(self):
        self.assertEqual(
            TransparentCompanySkill.CATEGORY_OPTIONS,
            (
                '通用',
                '金融',
                '法律',
                '新媒体创作',
                '办公写作',
                '电商/营销',
                '出行/生活',
                '医疗',
                '编程',
                '学术',
            ),
        )
        self.assertEqual(
            TransparentCompanySkill._meta.get_field('category').default,
            '通用',
        )

    def test_category_is_trimmed_and_blank_falls_back_to_default(self):
        serializer = TransparentCompanySkillUpsertSerializer(
            data={
                'cid': self.company.cid,
                'name': 'Contract review',
                'category': '  法律  ',
            },
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['category'], '法律')

        blank_serializer = TransparentCompanySkillUpsertSerializer(
            data={
                'cid': self.company.cid,
                'name': 'General assistant',
                'category': '   ',
            },
        )
        self.assertTrue(blank_serializer.is_valid(), blank_serializer.errors)
        self.assertEqual(blank_serializer.validated_data['category'], '通用')

    def test_generated_skill_ids_are_unique_within_the_same_second(self):
        with (
            patch.object(
                CompanyService,
                "_get_transparent_company_for_admin",
                return_value=(True, self.transparent_company),
            ),
            patch.object(
                CompanyService,
                "_sync_transparent_company_runtime_config",
                return_value=(True, {"status": "synced"}),
            ),
            patch("apps.company.services._moderate_company_texts", return_value=(True, "")),
            patch("apps.company.services.time.time", return_value=1_787_000_000),
        ):
            first_ok, first = self._create_skill("First skill")
            second_ok, second = self._create_skill("Second skill")

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertNotEqual(first["skillId"], second["skillId"])
        self.assertEqual(
            TransparentCompanySkill.objects.filter(
                transparentCompany=self.transparent_company,
            ).count(),
            2,
        )

    def test_retried_create_with_same_skill_id_is_idempotent(self):
        with (
            patch.object(
                CompanyService,
                '_get_transparent_company_for_admin',
                return_value=(True, self.transparent_company),
            ),
            patch.object(
                CompanyService,
                '_sync_transparent_company_runtime_config',
                return_value=(True, {'status': 'synced'}),
            ),
            patch('apps.company.services._moderate_company_texts', return_value=(True, '')),
        ):
            first_ok, first = self._create_skill('First response', 'skill-request-123')
            second_ok, second = self._create_skill('Retried response', 'skill-request-123')

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual(first['skillId'], second['skillId'])
        self.assertEqual(
            TransparentCompanySkill.objects.filter(
                transparentCompany=self.transparent_company,
                skillId='skill-request-123',
            ).count(),
            1,
        )

    def test_list_reports_enabled_agent_bindings(self):
        skill = self._create_skill_record('configured-skill')
        TransparentCompanyAgentSkillBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            skill=skill,
            enabled=True,
            created_at='1',
            updated_at='1',
        )


        ok, items = CompanyService.list_transparent_company_skills(
            self.company.cid,
            user=self.owner,
        )


        self.assertTrue(ok)
        item = next(row for row in items if row['skillId'] == skill.skillId)
        self.assertEqual(item['agentBindingCount'], 1)
        self.assertTrue(item['isConfigured'])

    def test_delete_custom_skill_removes_bindings(self):
        skill = self._create_skill_record('delete-me')
        skill_pk = skill.pk
        TransparentCompanyAgentSkillBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            skill=skill,
            enabled=True,
            created_at='1',
            updated_at='1',
        )
        with (
            patch.object(
                CompanyService,
                '_get_transparent_company_for_admin',
                return_value=(True, self.transparent_company),
            ),
            patch.object(
                CompanyService,
                '_sync_transparent_company_runtime_config',
                return_value=(True, {'status': 'synced'}),
            ),
        ):
            ok, payload = CompanyService.delete_transparent_company_skill(
                cid=self.company.cid,
                skill_id=skill.skillId,
                user=self.owner,
            )

        self.assertTrue(ok)
        self.assertEqual(payload['removedAgentCount'], 1)
        self.assertFalse(TransparentCompanySkill.objects.filter(pk=skill_pk).exists())
        self.assertFalse(
            TransparentCompanyAgentSkillBinding.objects.filter(skill_id=skill_pk).exists()
        )

    def test_system_skill_cannot_be_deleted(self):
        skill = self._create_skill_record('system-skill', system=True)
        with patch.object(
            CompanyService,
            '_get_transparent_company_for_admin',
            return_value=(True, self.transparent_company),
        ):
            ok, message = CompanyService.delete_transparent_company_skill(
                cid=self.company.cid,
                skill_id=skill.skillId,
                user=self.owner,
            )

        self.assertFalse(ok)
        self.assertEqual(message, '系统技能不可删除')
        self.assertTrue(TransparentCompanySkill.objects.filter(pk=skill.pk).exists())
