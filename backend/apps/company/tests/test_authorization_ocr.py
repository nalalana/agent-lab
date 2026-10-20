import base64
import io
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from apps.company.providers.manual import DocumentExtractionRequest
from apps.company.providers.tencent_manual import TencentManualVerificationProvider
from apps.company.verification_storage import (
    PrivateCosVerificationStorage,
    VerificationStorageError,
    validate_verification_storage_key,
)


class FakeOcrClient:
    def __init__(self):
        self.text_request = None
        self.seal_request = None

    def GeneralAccurateOCR(self, request):
        self.text_request = request
        point = SimpleNamespace(X=1, Y=2)
        detection = SimpleNamespace(
            DetectedText="测试企业有限公司",
            Confidence=98,
            Polygon=[point],
        )
        return SimpleNamespace(RequestId="text-request", TextDetections=[detection])

    def SealOCR(self, request):
        self.seal_request = request
        location = SimpleNamespace(X=3, Y=4, Width=20, Height=30)
        seal = SimpleNamespace(
            SealBody="测试企业有限公司",
            SealShape="0",
            Location=location,
        )
        return SimpleNamespace(RequestId="seal-request", SealInfos=[seal], SealBody="")


@override_settings(
    COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="mock",
    TENCENT_OCR_MAX_BASE64_BYTES=1024 * 1024,
)
class TencentAuthorizationOcrContractTests(SimpleTestCase):
    def test_pdf_requests_are_bounded_to_first_page_and_results_are_minimized(self):
        client = FakeOcrClient()
        provider = TencentManualVerificationProvider(
            registry_client=client,
            face_client=object(),
            ocr_client_instance=client,
        )
        content = b"%PDF-1.4 synthetic test payload"
        request = DocumentExtractionRequest(content=content, mime_type="application/pdf")

        text_result = provider.extract_document_text(request)
        seal_result = provider.extract_seal(request)

        expected = base64.b64encode(content).decode("ascii")
        self.assertEqual(client.text_request.ImageBase64, expected)
        self.assertTrue(client.text_request.IsPdf)
        self.assertEqual(client.text_request.PdfPageNumber, 1)
        self.assertEqual(client.seal_request.ImageBase64, expected)
        self.assertTrue(client.seal_request.EnablePdf)
        self.assertEqual(client.seal_request.PdfPageNumber, 1)
        self.assertEqual(text_result.outcome, "passed")
        self.assertEqual(text_result.lines[0]["text"], "测试企业有限公司")
        self.assertEqual(seal_result.outcome, "passed")
        self.assertEqual(seal_result.seals[0]["location"]["width"], 20)

    def test_unsupported_type_and_oversize_payload_never_call_sdk(self):
        client = FakeOcrClient()
        provider = TencentManualVerificationProvider(
            registry_client=client,
            face_client=object(),
            ocr_client_instance=client,
        )
        with self.assertRaises(Exception) as unsupported:
            provider.extract_document_text(
                DocumentExtractionRequest(content=b"x", mime_type="text/plain")
            )
        self.assertEqual(unsupported.exception.code, "unsupported_document_type")
        with override_settings(TENCENT_OCR_MAX_BASE64_BYTES=3):
            with self.assertRaises(Exception) as oversized:
                provider.extract_seal(
                    DocumentExtractionRequest(content=b"abcd", mime_type="image/png")
                )
        self.assertEqual(oversized.exception.code, "ocr_payload_too_large")
        self.assertIsNone(client.text_request)
        self.assertIsNone(client.seal_request)


class FakeCosClient:
    def __init__(self):
        self.put_kwargs = None
        self.deleted = []
        self.content = b"private-object"

    def put_object(self, **kwargs):
        self.put_kwargs = kwargs
        return {"ETag": '"etag-1"'}

    def get_object(self, **kwargs):
        return {"Content-Length": len(self.content), "Body": io.BytesIO(self.content)}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)


@override_settings(
    COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="mock",
    VERIFICATION_PRIVATE_COS_BUCKET="verification-private-123",
    VERIFICATION_PRIVATE_COS_REGION="ap-guangzhou",
)
class PrivateVerificationStorageTests(SimpleTestCase):
    def test_adapter_never_accepts_public_urls_or_cross_environment_keys(self):
        for value in (
            "https://example.test/document.pdf",
            "enterprise-verification/production/app/doc.pdf",
            "enterprise-verification/mock/../secret",
            "enterprise-verification/mock//document.pdf",
        ):
            with self.subTest(value=value), self.assertRaises(VerificationStorageError):
                validate_verification_storage_key(value, environment="mock")

    def test_private_adapter_uses_server_side_encryption_and_bounded_reads(self):
        client = FakeCosClient()
        storage = PrivateCosVerificationStorage(client=client)
        key = "enterprise-verification/mock/app/1/doc/original.png"
        stored = storage.put(key, b"content", content_type="application/octet-stream")
        self.assertEqual(stored.version, "etag-1")
        self.assertEqual(client.put_kwargs["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", client.put_kwargs)
        self.assertEqual(storage.get(key, max_bytes=100), b"private-object")
        with self.assertRaises(VerificationStorageError) as bounded:
            storage.get(key, max_bytes=2)
        self.assertEqual(bounded.exception.code, "storage_object_too_large")
        storage.delete(key)
        self.assertEqual(client.deleted[0]["Key"], key)
