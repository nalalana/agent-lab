from unittest.mock import patch

from django.db.utils import OperationalError
from django.test import RequestFactory, SimpleTestCase

from common.middleware import DevelopmentDatabaseRecoveryMiddleware


class DevelopmentDatabaseRecoveryMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("common.middleware.should_attempt_runtime_db_bootstrap", return_value=True)
    @patch("common.middleware.ensure_runtime_database_ready", return_value=(True, "bootstrapped"))
    def test_retries_request_after_bootstrap(self, ensure_mock, should_mock):
        state = {"count": 0}

        def get_response(request):
            state["count"] += 1
            if state["count"] == 1:
                raise OperationalError('database "touming_dev" does not exist')
            from django.http import JsonResponse
            return JsonResponse({"result": 1})

        middleware = DevelopmentDatabaseRecoveryMiddleware(get_response)
        response = middleware(self.factory.post("/api/company/transparent/agent-test/session/message"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(state["count"], 2)
        ensure_mock.assert_called_once()

    @patch("common.middleware.should_attempt_runtime_db_bootstrap", return_value=True)
    @patch("common.middleware.ensure_runtime_database_ready", return_value=(False, "failed"))
    def test_returns_safe_json_when_bootstrap_fails(self, ensure_mock, should_mock):
        def get_response(request):
            raise OperationalError('database "touming_dev" does not exist')

        middleware = DevelopmentDatabaseRecoveryMiddleware(get_response)
        response = middleware(self.factory.post("/api/company/transparent/agent-test/session/message"))

        self.assertEqual(response.status_code, 503)
        self.assertJSONEqual(
            response.content,
            {"result": 0, "message": "服务暂时不可用，请稍后重试", "code": 503},
        )
        ensure_mock.assert_called_once()
