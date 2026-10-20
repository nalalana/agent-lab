from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.moderation.models import ImageModerationAsset
from apps.user.models import User
from apps.user.services import UserService


class PublicImageBindingTests(TestCase):
    def test_legacy_avatar_url_requires_approved_moderation_asset(self):
        user = User.objects.create(
            uid="avatar-user",
            username="avatar-user",
            role="user",
            created_at="1",
            updated_at="1",
        )

        with self.assertRaises(ValidationError):
            UserService.upload_avatar(user.id, "uploads/untracked-avatar.png")

    def test_approved_avatar_key_can_be_bound(self):
        user = User.objects.create(
            uid="approved-avatar-user",
            username="approved-avatar-user",
            role="user",
            created_at="1",
            updated_at="1",
        )
        ImageModerationAsset.objects.create(
            storage_key="uploads/approved-avatar.png",
            source=ImageModerationAsset.SOURCE_USER_AVATAR,
            source_field="avatar",
            status=ImageModerationAsset.STATUS_APPROVED,
        )

        UserService.upload_avatar(user.id, "uploads/approved-avatar.png")
        user.refresh_from_db()

        self.assertEqual(user.avatar, "uploads/approved-avatar.png")
