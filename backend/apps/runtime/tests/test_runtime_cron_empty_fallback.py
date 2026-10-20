"""Compatibility tests for legacy runtime cron read endpoints during unified runtime rollout."""

import json
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from apps.company.models import Company
from apps.runtime.services import OpenClawRuntimeService
from apps.runtime.views import runtime_cron_job_list
from apps.user.models import User
from common.utils.Login_utils import sign_token


class RuntimeCronEmptyFallbackTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create(
            uid="runtime-cron-user",
            username="runtime-cron-user",
            created_at="2026-08-18",
            updated_at="2026-08-18",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="runtime-cron-company",
            company_name="Runtime Cron Company",
            created_at="2026-08-18",
            updated_at="2026-08-18",
        )
        self.token = sign_token({"user_id": self.user.id})

    def test_cron_job_list_returns_empty_when_legacy_runtime_instance_is_missing(self):
        request = self.factory.get(
            "/api/runtime/cron/jobs",
            {"cid": self.company.cid},
            HTTP_ACCESS_TOKEN=self.token,
        )

        with (
            patch.object(OpenClawRuntimeService, "assert_company_admin", return_value=(True, "")),
            patch.object(
                OpenClawRuntimeService,
                "get_company_instance",
                return_value=(False, "运行时实例不存在"),
            ),
            patch.object(OpenClawRuntimeService, "list_cron_jobs") as list_cron_jobs_mock,
        ):
            response = runtime_cron_job_list(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"], {"total": 0, "items": [], "hasMore": False})
        list_cron_jobs_mock.assert_not_called()