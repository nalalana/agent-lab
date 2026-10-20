import base64
import json
import os
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from apps.company.checks import enterprise_verification_configuration_check
from apps.company.retention_policy import RETENTION_POLICY_ID
from apps.company.verification_crypto import get_identity_protector
from config.enterprise_verification_secrets import load_enterprise_verification_secrets


ENCRYPTION_KEY = base64.b64encode(b"e" * 32).decode()
HMAC_KEY = base64.b64encode(b"h" * 32).decode()


def valid_payload():
    return {
        "tencent_secret_id": "secret-id",
        "tencent_secret_key": "secret-key",
        "faceid_rule_id": "rule-id",
        "faceid_redirect_url": "https://verify.example.test/enterprise-verification/face-return",
        "pii_encryption_key_b64": ENCRYPTION_KEY,
        "pii_hmac_key_b64": HMAC_KEY,
        "pii_key_version": "local-v1",
    }


class EnterpriseVerificationSecretLoaderTests(SimpleTestCase):
    def write_file(self, payload, *, mode=0o640):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "enterprise-verification.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_loads_strict_read_only_runtime_file(self):
        loaded = load_enterprise_verification_secrets(str(self.write_file(valid_payload())))
        self.assertEqual(loaded.error, "")
        self.assertEqual(set(loaded.values), set(valid_payload()))
        self.assertNotEqual(
            loaded.values["pii_encryption_key_b64"],
            loaded.values["pii_hmac_key_b64"],
        )

    def test_rejects_non_utf8_file(self):
        path = self.write_file(valid_payload())
        path.write_bytes(b"\xff")
        os.chmod(path, 0o640)
        loaded = load_enterprise_verification_secrets(str(path))
        self.assertEqual(loaded.error, "invalid_encoding")
        self.assertEqual(loaded.values, {})

    def test_rejects_group_writable_file(self):
        loaded = load_enterprise_verification_secrets(
            str(self.write_file(valid_payload(), mode=0o660))
        )
        self.assertEqual(loaded.error, "insecure_permissions")
        self.assertEqual(loaded.values, {})

    def test_rejects_world_readable_file(self):
        loaded = load_enterprise_verification_secrets(
            str(self.write_file(valid_payload(), mode=0o644))
        )
        self.assertEqual(loaded.error, "insecure_permissions")
        self.assertEqual(loaded.values, {})

    def test_rejects_missing_or_unexpected_fields(self):
        missing = valid_payload()
        missing.pop("faceid_rule_id")
        unexpected = valid_payload()
        unexpected["kms_key"] = "not-supported"
        self.assertEqual(
            load_enterprise_verification_secrets(str(self.write_file(missing))).error,
            "missing_keys",
        )
        self.assertEqual(
            load_enterprise_verification_secrets(str(self.write_file(unexpected))).error,
            "unexpected_keys",
        )

    def test_rejects_invalid_or_duplicate_pii_keys(self):
        invalid = valid_payload()
        invalid["pii_encryption_key_b64"] = base64.b64encode(b"short").decode()
        duplicate = valid_payload()
        duplicate["pii_hmac_key_b64"] = duplicate["pii_encryption_key_b64"]
        self.assertEqual(
            load_enterprise_verification_secrets(str(self.write_file(invalid))).error,
            "invalid_pii_key",
        )
        self.assertEqual(
            load_enterprise_verification_secrets(str(self.write_file(duplicate))).error,
            "duplicate_pii_keys",
        )

    def test_rejects_malformed_redirect(self):
        payload = valid_payload()
        payload["faceid_redirect_url"] = "localhost:3000/callback"
        loaded = load_enterprise_verification_secrets(str(self.write_file(payload)))
        self.assertEqual(loaded.error, "invalid_redirect_url")


@override_settings(
    COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="sandbox",
    COMPANY_VERIFICATION_ALLOW_MOCK=False,
    COMPANY_VERIFICATION_PII_ENCRYPTION_KEY=ENCRYPTION_KEY,
    COMPANY_VERIFICATION_PII_HMAC_KEY=HMAC_KEY,
    COMPANY_VERIFICATION_PII_KEY_VERSION="local-v1",
)
class LocalIdentityProtectorTests(SimpleTestCase):
    def test_production_style_keys_encrypt_and_decrypt_without_kms(self):
        protector = get_identity_protector()
        protected = protector.protect("11010519491231002X", context="application-id")
        self.assertNotIn("11010519491231002X", protected.ciphertext)
        self.assertEqual(protected.key_version, "local-v1")
        self.assertEqual(
            protector.decrypt(protected.ciphertext, context="application-id"),
            "11010519491231002X",
        )


@override_settings(
    COMPANY_VERIFICATION_ENABLED=True,
    COMPANY_VERIFICATION_PROVIDER="tencent_manual_v1",
    COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="sandbox",
    COMPANY_VERIFICATION_REGISTRY_MODE="two_elements",
    COMPANY_VERIFICATION_ALLOW_MOCK=False,
    COMPANY_VERIFICATION_SECRETS_LOAD_ERROR="",
    COMPANY_VERIFICATION_PII_ENCRYPTION_KEY=ENCRYPTION_KEY,
    COMPANY_VERIFICATION_PII_HMAC_KEY=HMAC_KEY,
    COMPANY_VERIFICATION_PII_KEY_VERSION="local-v1",
    TENCENT_VERIFICATION_SECRET_ID="secret-id",
    TENCENT_VERIFICATION_SECRET_KEY="secret-key",
    TENCENT_FACEID_RULE_ID="rule-id",
    TENCENT_FACEID_REDIRECT_URL="https://verify.example.test/enterprise-verification/face-return",
)
class SandboxConfigurationCheckTests(SimpleTestCase):
    def test_sandbox_is_ready_without_kms_cos_or_mock(self):
        self.assertEqual(enterprise_verification_configuration_check(None), [])

    @override_settings(
        COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="production",
        TENCENT_FACEID_REDIRECT_URL="http://203.0.113.10:3000/face-return",
        VERIFICATION_PRIVATE_COS_BUCKET="private-evidence-bucket",
        VERIFICATION_PRIVATE_COS_REGION="ap-guangzhou",
        VERIFICATION_RETENTION_POLICY=RETENTION_POLICY_ID,
    )
    def test_production_rejects_http_faceid_redirect(self):
        errors = enterprise_verification_configuration_check(None)
        self.assertIn("company.EV3005", {error.id for error in errors})
        self.assertNotIn("company.EV2011", {error.id for error in errors})

    @override_settings(
        COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="production",
        VERIFICATION_RETENTION_POLICY="unrecognized-policy",
    )
    def test_production_rejects_unrecognized_retention_policy(self):
        errors = enterprise_verification_configuration_check(None)
        self.assertIn("company.EV2011", {error.id for error in errors})
