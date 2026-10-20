"""Phase 2-C COS artifact reference and size-boundary tests."""

from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from django.test import SimpleTestCase

from apps.agent_runtime.providers.sandbox.artifacts import (
    ArtifactAccessDenied,
    ArtifactLimitExceeded,
    CosRunArtifactStore,
    truncate_utf8,
)


class FakeBody:
    def __init__(self, content):
        self.content = content

    def get_raw_stream(self):
        return BytesIO(self.content)


class FakeCosClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        content = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = (content, ContentType)
        return {"ETag": "fake"}

    def get_object(self, *, Bucket, Key):
        content, content_type = self.objects[(Bucket, Key)]
        return {
            "Content-Length": len(content),
            "Content-Type": content_type,
            "Body": FakeBody(content),
        }


class CosRunArtifactStoreTests(SimpleTestCase):
    def setUp(self):
        self.client = FakeCosClient()
        self.store = CosRunArtifactStore(
            client=self.client,
            bucket="runtime-artifacts",
            prefix="agent-runtime/v1",
            max_artifact_bytes=16,
        )
        self.run_id = str(uuid4())

    def test_round_trip_requires_same_company_and_run(self):
        ref = self.store.put(
            company_id="company-a",
            run_id=self.run_id,
            kind="input",
            name="safe input.txt",
            content=b"hello",
        )

        payload = self.store.get(ref, company_id="company-a", run_id=self.run_id)

        self.assertEqual(payload.content, b"hello")
        self.assertEqual(payload.name, "safe input.txt")
        with self.assertRaises(ArtifactAccessDenied):
            self.store.get(ref, company_id="company-b", run_id=self.run_id)
        with self.assertRaises(ArtifactAccessDenied):
            self.store.get(ref, company_id="company-a", run_id=str(uuid4()))

    def test_unicode_name_round_trips_without_changing_publish_identity(self):
        ref = self.store.put(
            company_id="company-a",
            run_id=self.run_id,
            kind="output",
            name="公司数据业务系统说明.txt",
            content=b"hello",
        )

        payload = self.store.get(ref, company_id="company-a", run_id=self.run_id)

        self.assertEqual(payload.name, "公司数据业务系统说明.txt")

    def test_unsafe_or_ambiguous_names_are_rejected(self):
        for name in ("../secret.txt", "dir/file.txt", "dir\\file.txt", ".", "..", "bad\x00.txt"):
            with self.subTest(name=name), self.assertRaises(ArtifactAccessDenied):
                self.store.put(
                    company_id="company-a",
                    run_id=self.run_id,
                    kind="output",
                    name=name,
                    content=b"hello",
                )

    def test_invalid_reference_and_large_object_are_rejected(self):
        with self.assertRaises(ArtifactAccessDenied):
            self.store.get(
                "https://cos.example/secret",
                company_id="company-a",
                run_id=self.run_id,
            )
        with self.assertRaises(ArtifactLimitExceeded):
            self.store.put(
                company_id="company-a",
                run_id=self.run_id,
                kind="output",
                name="large.bin",
                content=b"x" * 17,
            )

    def test_utf8_truncation_never_returns_partial_character(self):
        value, truncated = truncate_utf8("A中B", 3)

        self.assertEqual(value, "A")
        self.assertTrue(truncated)
