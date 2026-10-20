import json

from django.test import TestCase
from django.urls import reverse

from apps.moderation.models import ImageModerationAsset
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ImageModerationViewTests(TestCase):
    def _token(self, user: User) -> str:
        return sign_token({"user_id": user.id})

    def test_plain_user_cannot_manual_review(self):
        user = User.objects.create(
            uid="plain-review-user",
            username="plain",
            role="user",
            created_at="1",
            updated_at="1",
        )
        ImageModerationAsset.objects.create(
            storage_key="uploads/review.png",
            source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
            source_field="logo",
            status=ImageModerationAsset.STATUS_PENDING,
        )

        response = self.client.post(
            reverse("image-moderation-review"),
            data=json.dumps({"storage_key": "uploads/review.png", "action": "approve"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=self._token(user),
        )

        self.assertEqual(response.status_code, 403)
    def test_cos_callback_requires_configured_secret(self):
        ImageModerationAsset.objects.create(
            storage_key="uploads/callback.png",
            source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
            source_field="logo",
            status=ImageModerationAsset.STATUS_PENDING,
        )

        response = self.client.post(
            reverse("image-moderation-cos-callback"),
            data=json.dumps({"JobsDetail": {"Object": "uploads/callback.png", "Suggestion": "Pass"}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
