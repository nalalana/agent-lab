"""Phase 1-C tests for OpenAI-compatible model event normalization."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.agent_runtime.contracts import ModelRequest
from apps.agent_runtime.enums import ErrorCode, ModelEventType
from apps.agent_runtime.providers.model import (
    CompanyModelResolver,
    DjangoModelCredentialLoader,
    ModelConfigurationError,
    ModelGatewayRegistry,
    OpenAICompatibleModelGateway,
    ResolvedCredential,
)


class PlatformModelResolverTests(SimpleTestCase):
    @override_settings(
        AGENT_RUNTIME_PLATFORM_PROVIDER_ID="custom-qwen",
        AGENT_RUNTIME_PLATFORM_MODEL_ID="qwen3.8-27b",
    )
    def test_selection_does_not_require_catalog_rows(self):
        selection = CompanyModelResolver().resolve(SimpleNamespace())

        self.assertEqual(selection.provider, "custom-qwen")
        self.assertEqual(selection.model, "qwen3.8-27b")
        self.assertEqual(selection.credential_ref, "platform:custom-qwen")


class OpenAICompatibleModelGatewayTests(SimpleTestCase):
    def test_normalizes_text_tool_usage_and_finish_events(self):
        chunks = [
            _chunk(
                content="Hel",
                tool_calls=[_tool_fragment(0, "call-1", "search", '{"q":')],
            ),
            _chunk(
                content="lo",
                tool_calls=[_tool_fragment(0, "", "", '"phase1"}')],
                finish_reason="tool_calls",
                usage=(7, 3, 10),
            ),
        ]
        client = FakeClient(chunks)
        gateway = OpenAICompatibleModelGateway(
            credential_loader=FakeCredentialLoader(),
            client_factory=lambda _credential, _request: client,
            max_retries=0,
        )

        events = asyncio.run(_collect(gateway.stream(_request())))

        self.assertEqual(
            [event.event_type for event in events],
            [
                str(ModelEventType.TEXT_DELTA),
                str(ModelEventType.TOOL_CALL_DELTA),
                str(ModelEventType.TEXT_DELTA),
                str(ModelEventType.TOOL_CALL_DELTA),
                str(ModelEventType.TOOL_CALL_COMPLETED),
                str(ModelEventType.USAGE),
                str(ModelEventType.CALL_METRICS),
                str(ModelEventType.FINISH),
            ],
        )
        self.assertEqual(events[4].data["arguments"], '{"q":"phase1"}')
        self.assertEqual(events[5].data["total_tokens"], 10)
        self.assertEqual(events[6].data["purpose"], "generation")
        self.assertEqual(events[6].data["total_tokens"], 10)
        self.assertEqual(events[7].data["reason"], "tool_calls")
        self.assertEqual(client.create_kwargs["stream_options"], {"include_usage": True})
        self.assertTrue(client.closed)
        self.assertTrue(client.stream.closed)

    @override_settings(
        AGENT_RUNTIME_PLATFORM_PROVIDER_ID="custom-qwen",
        AGENT_RUNTIME_PLATFORM_DISABLE_THINKING=True,
    )
    def test_custom_qwen_disables_thinking_in_request_body(self):
        payload = OpenAICompatibleModelGateway._request_payload(
            _request(provider="custom-qwen")
        )

        self.assertEqual(
            payload["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )

    def test_retries_retryable_failure_only_before_first_event(self):
        successful_client = FakeClient([_chunk(content="ok", finish_reason="stop")])
        attempts = []

        def client_factory(_credential, _request):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise ProviderFailure(429, "upstream body must not escape")
            return successful_client

        gateway = OpenAICompatibleModelGateway(
            credential_loader=FakeCredentialLoader(),
            client_factory=client_factory,
            max_retries=1,
            retry_base_delay=0,
        )

        events = asyncio.run(_collect(gateway.stream(_request())))

        self.assertEqual(len(attempts), 2)
        self.assertEqual(events[0].event_type, str(ModelEventType.TEXT_DELTA))
        self.assertEqual(events[-1].event_type, str(ModelEventType.FINISH))

    def test_does_not_retry_or_leak_error_after_observable_event(self):
        failing_client = FakeClient(
            [_chunk(content="partial")],
            terminal_error=ProviderFailure(429, "secret-key-in-upstream-body"),
        )
        factory_calls = []
        gateway = OpenAICompatibleModelGateway(
            credential_loader=FakeCredentialLoader(),
            client_factory=lambda _credential, _request: (
                factory_calls.append(True) or failing_client
            ),
            max_retries=2,
            retry_base_delay=0,
        )

        events = asyncio.run(_collect(gateway.stream(_request())))

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(events[0].data["delta"], "partial")
        self.assertEqual(events[-1].event_type, str(ModelEventType.ERROR))
        self.assertEqual(events[-1].data["code"], str(ErrorCode.MODEL_RATE_LIMITED))
        self.assertNotIn("secret-key", str(events[-1].data))

    def test_parses_split_minimax_text_tool_call_without_exposing_markup(self):
        chunks = [
            _chunk(content="<think>先查看文件</think>\n<minimax:tool_"),
            _chunk(content="call>\n<invoke name=\"workspace.list\">\n</invoke>"),
            _chunk(content="\n</minimax:tool_call>", finish_reason="stop"),
        ]
        gateway = OpenAICompatibleModelGateway(
            credential_loader=FakeCredentialLoader(),
            client_factory=lambda _credential, _request: FakeClient(chunks),
            max_retries=0,
        )

        events = asyncio.run(_collect(gateway.stream(_request(provider="minimax"))))

        visible = "".join(
            event.data["delta"]
            for event in events
            if event.event_type == str(ModelEventType.TEXT_DELTA)
        )
        calls = [event for event in events if event.event_type == str(ModelEventType.TOOL_CALL_COMPLETED)]
        self.assertEqual(visible, "<think>先查看文件</think>\n")
        self.assertNotIn("minimax:tool_call", visible)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].data["name"], "workspace.list")
        self.assertEqual(calls[0].data["arguments"], "{}")

    def test_parses_minimax_parameters_for_any_registered_tool(self):
        chunks = [_chunk(
            content=(
                '<minimax:tool_call><invoke name="workspace.read">'
                '<parameter name="logical_path">knowledge/a.md</parameter>'
                '<parameter name="unused_boolean">true</parameter>'
                '</invoke></minimax:tool_call>'
            ),
            finish_reason="stop",
        )]
        gateway = OpenAICompatibleModelGateway(
            credential_loader=FakeCredentialLoader(),
            client_factory=lambda _credential, _request: FakeClient(chunks),
            max_retries=0,
        )

        events = asyncio.run(_collect(gateway.stream(_request(provider="minimax"))))
        call = next(event for event in events if event.event_type == str(ModelEventType.TOOL_CALL_COMPLETED))

        self.assertEqual(call.data["name"], "workspace.read")
        self.assertEqual(
            call.data["arguments"],
            '{"logical_path":"knowledge/a.md","unused_boolean":true}',
        )


class ModelGatewayRegistryTests(SimpleTestCase):
    def test_exact_provider_precedes_wildcard(self):
        registry = ModelGatewayRegistry()
        fallback = object()
        exact = object()
        registry.register("*", fallback)
        registry.register("OpenAI", exact)

        self.assertIs(registry.get("openai"), exact)
        self.assertIs(registry.get("another-provider"), fallback)

    def test_missing_provider_registration_fails_safely(self):
        with self.assertRaises(ModelConfigurationError):
            ModelGatewayRegistry().get("unknown")


class ModelCredentialUrlValidationTests(SimpleTestCase):
    def test_rejects_insecure_and_private_provider_urls_by_default(self):
        with self.assertRaises(ModelConfigurationError):
            DjangoModelCredentialLoader._validated_base_url("http://models.example/v1")
        with self.assertRaises(ModelConfigurationError):
            DjangoModelCredentialLoader._validated_base_url("https://127.0.0.1/v1")
        with self.assertRaises(ModelConfigurationError):
            DjangoModelCredentialLoader._validated_base_url("https://localhost/v1")

    @override_settings(AGENT_RUNTIME_MODEL_ALLOWED_HOSTS=("models.internal",))
    def test_explicit_allowlist_supports_trusted_internal_gateway(self):
        self.assertEqual(
            DjangoModelCredentialLoader._validated_base_url(
                "https://models.internal/v1/"
            ),
            "https://models.internal/v1",
        )
        with self.assertRaises(ModelConfigurationError):
            DjangoModelCredentialLoader._validated_base_url("https://other.example/v1")


class ModelCredentialEnvironmentTests(SimpleTestCase):
    @override_settings(AGENT_RUNTIME_MODEL_API_KEYS={})
    def test_custom_provider_falls_back_to_generic_platform_key(self):
        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_PLATFORM_API_KEY": "generic-platform-key"},
            clear=True,
        ):
            self.assertEqual(
                DjangoModelCredentialLoader._platform_key("custom-qwen"),
                "generic-platform-key",
            )

    @override_settings(AGENT_RUNTIME_MODEL_API_KEYS={})
    def test_provider_specific_key_precedes_generic_platform_key(self):
        with patch.dict(
            os.environ,
            {
                "CUSTOM_QWEN_API_KEY": "provider-key",
                "AGENT_RUNTIME_PLATFORM_API_KEY": "generic-platform-key",
            },
            clear=True,
        ):
            self.assertEqual(
                DjangoModelCredentialLoader._platform_key("custom-qwen"),
                "provider-key",
            )


class FakeCredentialLoader:
    def load(self, credential_ref, provider):
        return ResolvedCredential(api_key="test-only-key", base_url="https://models.example")


class FakeClient:
    def __init__(self, chunks, terminal_error=None):
        self.stream = FakeStream(chunks, terminal_error=terminal_error)
        self.create_kwargs = None
        self.closed = False
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self.stream

    async def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, chunks, terminal_error=None):
        self.chunks = iter(chunks)
        self.terminal_error = terminal_error
        self.raised_terminal_error = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.terminal_error is not None and not self.raised_terminal_error:
                self.raised_terminal_error = True
                raise self.terminal_error
            raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class ProviderFailure(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


async def _collect(source):
    return [event async for event in source]


def _request(*, provider="openai"):
    return ModelRequest(
        run_id="run-1",
        provider=provider,
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
        temperature=0.2,
        max_output_tokens=128,
        timeout_seconds=5,
        credential_ref="platform:openai",
    )


def _chunk(*, content="", tool_calls=None, finish_reason=None, usage=None):
    usage_payload = None
    if usage is not None:
        usage_payload = SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=usage_payload,
    )


def _tool_fragment(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
