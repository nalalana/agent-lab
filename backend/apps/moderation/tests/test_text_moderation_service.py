from unittest.mock import patch

from django.test import TestCase

from apps.iam.models import Grant, Principal
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.moderation.models import SensitiveWordRule, TextModerationAsset
from apps.moderation.services import TextModerationService
from apps.user.models import User


class TextModerationServiceTests(TestCase):
    def test_disabled_provider_records_and_approves_for_unconfigured_development(self):
        with patch.dict("os.environ", {"TEXT_MODERATION_PROVIDER": "disabled"}, clear=False):
            asset = TextModerationService.check_text(
                "hello world",
                source=TextModerationAsset.SOURCE_COMPANY_PROFILE,
                source_field="description",
            )

        self.assertEqual(asset.status, TextModerationAsset.STATUS_APPROVED)
        self.assertEqual(asset.provider, TextModerationAsset.PROVIDER_DISABLED)
        self.assertEqual(asset.content_preview, "hello world")
        self.assertEqual(asset.events.count(), 1)

    def test_env_sensitive_word_rejects_before_provider(self):
        with patch.dict(
            "os.environ",
            {
                "TEXT_MODERATION_PROVIDER": "disabled",
                "TEXT_MODERATION_SENSITIVE_WORDS": "forbidden phrase",
            },
            clear=False,
        ):
            asset = TextModerationService.check_text(
                "this contains forbidden phrase",
                source=TextModerationAsset.SOURCE_OPENGATE_TABLE,
                source_field="cell",
            )

        self.assertEqual(asset.status, TextModerationAsset.STATUS_REJECTED)
        ok, message = TextModerationService.require_approved_public_text(asset)
        self.assertFalse(ok)
        self.assertIn("TEXT_MODERATION_BLOCKED", message)

    def test_database_sensitive_word_can_route_to_manual_review(self):
        SensitiveWordRule.objects.create(
            word="needs review",
            normalized_word="needsreview",
            action=SensitiveWordRule.ACTION_REVIEW_REQUIRED,
            label="custom",
        )

        asset = TextModerationService.check_text(
            "This needs review please",
            source=TextModerationAsset.SOURCE_AGENT_PROMPT,
            source_field="systemPrompt",
        )

        self.assertEqual(asset.status, TextModerationAsset.STATUS_REVIEW_REQUIRED)
        self.assertEqual(asset.label, "custom")

    def test_company_admin_can_manually_review_company_owned_text(self):
        user = User.objects.create(
            uid="company-admin",
            username="admin",
            role="user",
            created_at="1",
            updated_at="1",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
        resource = get_or_create_resource("company", "cid-1")
        grant_permissions(principal, resource, Grant.PERM_ADMIN)
        asset = TextModerationAsset.objects.create(
            content_hash="abc",
            content_preview="pending",
            source=TextModerationAsset.SOURCE_COMPANY_PROFILE,
            source_field="description",
            owner_type="company",
            owner_id="cid-1",
            status=TextModerationAsset.STATUS_REVIEW_REQUIRED,
        )

        self.assertTrue(TextModerationService.can_manual_review(user, asset))

    def test_actor_can_view_own_text_status(self):
        user = User.objects.create(
            uid="actor-user",
            username="actor",
            role="user",
            created_at="1",
            updated_at="1",
        )
        asset = TextModerationAsset.objects.create(
            content_hash="actor-hash",
            content_preview="preview",
            source=TextModerationAsset.SOURCE_PUBLIC_CLIENT_ENTRY,
            source_field="content",
            actor_type="user",
            actor_id=user.uid,
            status=TextModerationAsset.STATUS_REVIEW_REQUIRED,
        )

        self.assertTrue(TextModerationService.can_view_status(user, asset))

    def test_unrelated_user_cannot_view_text_status(self):
        user = User.objects.create(
            uid="unrelated-user",
            username="unrelated",
            role="user",
            created_at="1",
            updated_at="1",
        )
        asset = TextModerationAsset.objects.create(
            content_hash="other-hash",
            content_preview="preview",
            source=TextModerationAsset.SOURCE_PUBLIC_CLIENT_ENTRY,
            source_field="content",
            actor_type="user",
            actor_id="other-user",
            status=TextModerationAsset.STATUS_REVIEW_REQUIRED,
        )

        self.assertFalse(TextModerationService.can_view_status(user, asset))
