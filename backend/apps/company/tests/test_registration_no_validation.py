from django.test import SimpleTestCase

from apps.company.serializers import (
    CompanyCreateSerializer,
    CompanyUpdateSerializer,
)
from apps.company.validators import normalize_social_credit_code


VALID_SOCIAL_CREDIT_CODE = '91350211M000100Y46'


def company_create_payload(registration_no=VALID_SOCIAL_CREDIT_CODE):
    return {
        'company_name': 'Demo Company',
        'registration_no': registration_no,
        'company_size': '10-50',
        'contact_phone': '13800138000',
        'contact_email': 'demo@example.com',
        'address': 'Demo Address',
    }


class CompanyRegistrationNoValidationTests(SimpleTestCase):
    def test_create_accepts_valid_social_credit_code(self):
        serializer = CompanyCreateSerializer(data=company_create_payload())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['registration_no'], VALID_SOCIAL_CREDIT_CODE)

    def test_create_normalizes_spaces_hyphens_and_lowercase(self):
        serializer = CompanyCreateSerializer(data=company_create_payload('91350211-m000100y46'))

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['registration_no'], VALID_SOCIAL_CREDIT_CODE)

    def test_create_rejects_invalid_length_or_charset(self):
        for value in ('91350211M000100Y4', '91350211M000100Y4Z'):
            serializer = CompanyCreateSerializer(data=company_create_payload(value))

            self.assertFalse(serializer.is_valid())
            self.assertIn('registration_no', serializer.errors)

    def test_create_rejects_invalid_check_digit(self):
        serializer = CompanyCreateSerializer(data=company_create_payload('91350211M000100Y45'))

        self.assertFalse(serializer.is_valid())
        self.assertIn('registration_no', serializer.errors)

    def test_update_allows_blank_but_validates_present_value(self):
        blank_serializer = CompanyUpdateSerializer(data={'cid': 'cid-1', 'registration_no': ''})
        invalid_serializer = CompanyUpdateSerializer(data={'cid': 'cid-1', 'registration_no': '91350211M000100Y45'})

        self.assertTrue(blank_serializer.is_valid(), blank_serializer.errors)
        self.assertEqual(blank_serializer.validated_data['registration_no'], '')
        self.assertFalse(invalid_serializer.is_valid())
        self.assertIn('registration_no', invalid_serializer.errors)

    def test_normalize_social_credit_code(self):
        self.assertEqual(normalize_social_credit_code('91350211 m000100y46'), VALID_SOCIAL_CREDIT_CODE)
