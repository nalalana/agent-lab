from unittest.mock import patch

from django.test import TestCase

from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService
from apps.user.models import User


class ImageModerationServiceTests(TestCase):
    def test_new_uploaded_image_defaults_to_approved_without_env(self):
        asset = ImageModerationService.register_uploaded_image(
            "uploads/default-approved.png",
            source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
            source_field="logo",
            mime_type="image/png",
            size=123,
        )

        self.assertEqual(asset.status, ImageModerationAsset.STATUS_APPROVED)
        self.assertTrue(ImageModerationService.is_publicly_viewable("uploads/default-approved.png", "image/png"))

    def test_new_uploaded_image_defaults_to_pending_when_provider_disabled(self):
        with patch.dict("os.environ", {"IMAGE_MODERATION_PROVIDER": "disabled"}, clear=False):
            asset = ImageModerationService.register_uploaded_image(
                "uploads/demo.png",
                source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
                source_field="logo",
                mime_type="image/png",
                size=123,
            )

        self.assertEqual(asset.status, ImageModerationAsset.STATUS_PENDING)
        self.assertTrue(ImageModerationService.is_publicly_viewable("uploads/demo.png", "image/png"))
        with patch("apps.moderation.services.get_public_cdn_url", return_value="https://cdn.example.test/uploads/demo.png") as get_url:
            self.assertEqual(ImageModerationService.get_public_image_url("uploads/demo.png"), "https://cdn.example.test/uploads/demo.png")
        get_url.assert_called_once_with("uploads/demo.png")

    def test_approved_image_can_receive_public_url(self):
        asset = ImageModerationAsset.objects.create(
            storage_key="uploads/approved.jpg",
            source=ImageModerationAsset.SOURCE_USER_AVATAR,
            source_field="avatar",
            mime_type="image/jpeg",
            status=ImageModerationAsset.STATUS_APPROVED,
        )

        with patch("apps.moderation.services.get_public_cdn_url", return_value="https://cdn.example.test/uploads/approved.jpg") as get_url:
            url = ImageModerationService.get_public_image_url(asset.storage_key)

        self.assertEqual(url, "https://cdn.example.test/uploads/approved.jpg")
        get_url.assert_called_once_with("uploads/approved.jpg")

    def test_untracked_images_are_blocked_by_default(self):
        self.assertFalse(ImageModerationService.is_publicly_viewable("legacy/logo.png", "image/png"))

    def test_untracked_images_can_be_allowed_explicitly_for_legacy_development(self):
        with patch.dict("os.environ", {"IMAGE_MODERATION_ALLOW_UNTRACKED": "true"}, clear=False):
            self.assertTrue(ImageModerationService.is_publicly_viewable("legacy/logo.png", "image/png"))

    def test_local_provider_can_auto_approve_for_development(self):
        with patch.dict(
            "os.environ",
            {
                "IMAGE_MODERATION_PROVIDER": "local",
                "IMAGE_MODERATION_LOCAL_DECISION": ImageModerationAsset.STATUS_APPROVED,
            },
            clear=False,
        ):
            asset = ImageModerationService.register_uploaded_image(
                "uploads/local-pass.webp",
                source=ImageModerationAsset.SOURCE_OPENGATE_IMAGE,
                source_field="background_image",
                mime_type="image/webp",
                size=456,
            )

        self.assertEqual(asset.status, ImageModerationAsset.STATUS_APPROVED)

    def test_public_image_binding_requires_approved_asset(self):
        ok, _key, message = ImageModerationService.require_approved_public_image("uploads/not-tracked.png")

        self.assertFalse(ok)
        self.assertIn("审核记录", message)

    def test_disabled_provider_allows_pending_image_binding(self):
        ImageModerationAsset.objects.create(
            storage_key="uploads/pending-disabled.png",
            source=ImageModerationAsset.SOURCE_USER_AVATAR,
            source_field="avatar",
            status=ImageModerationAsset.STATUS_PENDING,
        )

        with patch.dict("os.environ", {"IMAGE_MODERATION_PROVIDER": "disabled"}, clear=False):
            ok, key, message = ImageModerationService.require_approved_public_image("uploads/pending-disabled.png")

        self.assertTrue(ok)
        self.assertEqual(key, "uploads/pending-disabled.png")
        self.assertEqual(message, "")

    def test_disabled_provider_still_blocks_rejected_image(self):
        ImageModerationAsset.objects.create(
            storage_key="uploads/rejected-disabled.png",
            source=ImageModerationAsset.SOURCE_USER_AVATAR,
            source_field="avatar",
            status=ImageModerationAsset.STATUS_REJECTED,
        )

        with patch.dict("os.environ", {"IMAGE_MODERATION_PROVIDER": "disabled"}, clear=False):
            ok, _key, message = ImageModerationService.require_approved_public_image("uploads/rejected-disabled.png")

        self.assertFalse(ok)
        self.assertIn("未审核通过", message)

    def test_root_or_admin_can_manually_review_any_asset(self):
        user = User.objects.create(
            uid="root-user",
            username="root",
            role="root",
            created_at="1",
            updated_at="1",
        )
        asset = ImageModerationAsset.objects.create(
            storage_key="uploads/pending.png",
            source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
            source_field="logo",
            status=ImageModerationAsset.STATUS_PENDING,
        )

        self.assertTrue(ImageModerationService.can_manual_review(user, asset))

    def test_plain_user_cannot_manually_review_user_scoped_asset(self):
        user = User.objects.create(
            uid="plain-user",
            username="plain",
            role="user",
            created_at="1",
            updated_at="1",
        )
        asset = ImageModerationAsset.objects.create(
            storage_key="uploads/pending-user.png",
            source=ImageModerationAsset.SOURCE_USER_AVATAR,
            source_field="avatar",
            owner_type="user",
            owner_id=user.uid,
            status=ImageModerationAsset.STATUS_PENDING,
        )

        self.assertFalse(ImageModerationService.can_manual_review(user, asset))
