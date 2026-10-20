from unittest.mock import patch

from django.test import SimpleTestCase

from apps.user.serializers import PhoneRegisterSerializer, UserUpdateSerializer


class UserUpdateSerializerTests(SimpleTestCase):
    @patch("apps.user.serializers.User.objects.filter")
    def test_blank_email_skips_duplicate_check(self, filter_mock):
        serializer = UserUpdateSerializer(
            data={"email": ""},
            context={"user_id": 999999},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["email"], "")
        filter_mock.assert_not_called()

    @patch("apps.user.serializers.User.objects.filter")
    def test_email_is_trimmed_before_duplicate_check(self, filter_mock):
        filter_mock.return_value.exclude.return_value.exists.return_value = False
        serializer = UserUpdateSerializer(
            data={"email": "  used@example.com  "},
            context={"user_id": 42},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["email"], "used@example.com")
        filter_mock.assert_called_once_with(email="used@example.com")
        filter_mock.return_value.exclude.assert_called_once_with(id=42)


class PhoneRegisterAgreementTests(SimpleTestCase):
    @patch("apps.user.serializers.User.objects.filter")
    def test_registration_requires_agreement(self, filter_mock):
        filter_mock.return_value.exists.return_value = False
        serializer = PhoneRegisterSerializer(data={"phone": "13800138000", "code": "123456", "agreed_to_terms": False})

        self.assertFalse(serializer.is_valid())
        self.assertIn("请先阅读并同意用户协议和隐私政策", str(serializer.errors))
