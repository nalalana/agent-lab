from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


DEFAULT_OPENCLAW_VERSION = "2026.4.8"


@dataclass(frozen=True)
class OpenClawCatalogSnapshot:
    version: str
    provider_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    package_ids: tuple[str, ...] = ()


# OpenClaw 的模型目录来自运行时 provider directory。这里维护的是和当前版本对齐的
# “公开可选目录 + 常用推荐模型”，不是最终运行时的唯一限制条件。
# 对于支持动态模型解析的 provider，平台允许管理员输入自定义模型 ID。
OPENCLAW_MODEL_CATALOGS: dict[str, OpenClawCatalogSnapshot] = {
    DEFAULT_OPENCLAW_VERSION: OpenClawCatalogSnapshot(
        version=DEFAULT_OPENCLAW_VERSION,
        provider_ids=(
            "custom-qwen",
            "openai",
            "anthropic",
            "openai-codex",
            "opencode",
            "opencode-go",
            "google",
            "google-vertex",
            "google-gemini-cli",
            "zai",
            "vercel-ai-gateway",
            "kilocode",
            "openrouter",
            "minimax",
            "moonshot",
            "kimi-coding",
            "qianfan",
            "modelstudio",
            "qwen",
            "deepseek",
            "nvidia",
            "together",
            "venice",
            "xiaomi",
            "huggingface",
            "cloudflare-ai-gateway",
            "volcengine",
            "doubao",
            "byteplus",
            "xai",
            "mistral",
            "groq",
            "cerebras",
            "github-copilot",
            "ollama",
            "vllm",
            "sglang",
            "synthetic",
            "glm",
        ),
        model_ids=(
            "qwen3.8-27b",
            "gpt-4.1-mini",
            "gpt-5.4",
            "gpt-5.4-pro",
            "gpt-5.3-codex-spark",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "glm-5",
            "glm-4-plus",
            "deepseek-chat",
            "deepseek-reasoner",
            "qwen-max",
            "qwen-plus",
            "qwen-turbo",
            "kimi-k2.5",
            "kimi-k2-0905-preview",
            "kimi-k2-turbo-preview",
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "k2p5",
            "mistral-large-latest",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "zai-glm-4.7",
            "zai-glm-4.6",
            "deepseek-ai/DeepSeek-R1",
            "grok-code-fast-1",
            "glm-5-free",
            "MiniMax-M2.5",
        ),
    ),
}


OPENCLAW_MODEL_ID_ALIASES: dict[str, dict[str, str]] = {
    DEFAULT_OPENCLAW_VERSION: {
        "m2.5": "MiniMax-M2.5",
    },
}


OPENCLAW_PROVIDER_METADATA: dict[str, dict[str, dict[str, object]]] = {
    DEFAULT_OPENCLAW_VERSION: {
        "custom-qwen": {"name": "Platform Qwen3.8", "slug": "custom-qwen", "baseUrl": "http://112.95.75.67:9018/v1", "authMode": "platform_key", "isPlatformManaged": True},
        "openai": {"name": "OpenAI", "slug": "openai", "authMode": "api_key", "isPlatformManaged": True},
        "anthropic": {"name": "Anthropic", "slug": "anthropic", "authMode": "api_key", "isPlatformManaged": True},
        "openai-codex": {"name": "OpenAI Codex", "slug": "openai-codex", "authMode": "oauth", "isPlatformManaged": True},
        "opencode": {"name": "OpenCode", "slug": "opencode", "authMode": "api_key", "isPlatformManaged": True},
        "opencode-go": {"name": "OpenCode Go", "slug": "opencode-go", "authMode": "api_key", "isPlatformManaged": True},
        "google": {"name": "Google Gemini", "slug": "google", "authMode": "api_key", "isPlatformManaged": True},
        "google-vertex": {"name": "Google Vertex", "slug": "google-vertex", "authMode": "adc", "isPlatformManaged": True},
        "google-gemini-cli": {"name": "Google Gemini CLI", "slug": "google-gemini-cli", "authMode": "oauth", "isPlatformManaged": True},
        "zai": {"name": "Z.AI (GLM)", "slug": "zai", "authMode": "api_key", "isPlatformManaged": True},
        "vercel-ai-gateway": {"name": "Vercel AI Gateway", "slug": "vercel-ai-gateway", "authMode": "api_key", "isPlatformManaged": True},
        "kilocode": {"name": "Kilo Gateway", "slug": "kilocode", "authMode": "api_key", "isPlatformManaged": True},
        "openrouter": {"name": "OpenRouter", "slug": "openrouter", "authMode": "api_key", "isPlatformManaged": True},
        "minimax": {"name": "MiniMax", "slug": "minimax", "baseUrl": "https://api.minimaxi.com/v1", "authMode": "api_key", "isPlatformManaged": True},
        "moonshot": {"name": "Moonshot AI", "slug": "moonshot", "authMode": "api_key", "isPlatformManaged": True},
        "kimi-coding": {"name": "Kimi Coding", "slug": "kimi-coding", "authMode": "api_key", "isPlatformManaged": True},
        "qianfan": {"name": "Qianfan", "slug": "qianfan", "authMode": "api_key", "isPlatformManaged": True},
        "modelstudio": {"name": "Model Studio", "slug": "modelstudio", "authMode": "api_key", "isPlatformManaged": True},
        "qwen": {"name": "Qwen", "slug": "qwen", "authMode": "api_key", "isPlatformManaged": True},
        "deepseek": {"name": "DeepSeek", "slug": "deepseek", "authMode": "api_key", "isPlatformManaged": True},
        "nvidia": {"name": "NVIDIA", "slug": "nvidia", "authMode": "api_key", "isPlatformManaged": True},
        "together": {"name": "Together AI", "slug": "together", "authMode": "api_key", "isPlatformManaged": True},
        "venice": {"name": "Venice AI", "slug": "venice", "authMode": "api_key", "isPlatformManaged": True},
        "xiaomi": {"name": "Xiaomi MiMo", "slug": "xiaomi", "authMode": "api_key", "isPlatformManaged": True},
        "huggingface": {"name": "Hugging Face", "slug": "huggingface", "authMode": "api_key", "isPlatformManaged": True},
        "cloudflare-ai-gateway": {"name": "Cloudflare AI Gateway", "slug": "cloudflare-ai-gateway", "authMode": "api_key", "isPlatformManaged": True},
        "volcengine": {"name": "Volcengine (Doubao)", "slug": "volcengine", "authMode": "api_key", "isPlatformManaged": True},
        "doubao": {"name": "Doubao", "slug": "doubao", "authMode": "api_key", "isPlatformManaged": True},
        "byteplus": {"name": "BytePlus", "slug": "byteplus", "authMode": "api_key", "isPlatformManaged": True},
        "xai": {"name": "xAI", "slug": "xai", "authMode": "api_key", "isPlatformManaged": True},
        "mistral": {"name": "Mistral", "slug": "mistral", "authMode": "api_key", "isPlatformManaged": True},
        "groq": {"name": "Groq", "slug": "groq", "authMode": "api_key", "isPlatformManaged": True},
        "cerebras": {"name": "Cerebras", "slug": "cerebras", "authMode": "api_key", "isPlatformManaged": True},
        "github-copilot": {"name": "GitHub Copilot", "slug": "github-copilot", "authMode": "oauth", "isPlatformManaged": True},
        "ollama": {"name": "Ollama", "slug": "ollama", "authMode": "none", "isPlatformManaged": True},
        "vllm": {"name": "vLLM", "slug": "vllm", "authMode": "none", "isPlatformManaged": True},
        "sglang": {"name": "SGLang", "slug": "sglang", "authMode": "none", "isPlatformManaged": True},
        "synthetic": {"name": "Synthetic", "slug": "synthetic", "authMode": "api_key", "isPlatformManaged": True},
        "glm": {"name": "GLM", "slug": "glm", "authMode": "api_key", "isPlatformManaged": True},
    },
}


OPENCLAW_MODEL_METADATA: dict[str, dict[str, dict[str, object]]] = {
    DEFAULT_OPENCLAW_VERSION: {
        "qwen3.8-27b": {"providerId": "custom-qwen", "displayName": "Qwen3.8 27B", "category": "chat", "supportsTools": True, "contextWindow": 131072, "isPlatformDefault": True},
        "gpt-4.1-mini": {"providerId": "openai", "displayName": "GPT-4.1 Mini", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "gpt-5.4": {"providerId": "openai", "displayName": "GPT-5.4", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "gpt-5.4-pro": {"providerId": "openai", "displayName": "GPT-5.4 Pro", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "gpt-5.3-codex-spark": {"providerId": "openai-codex", "displayName": "GPT-5.3 Codex Spark", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "claude-opus-4-6": {"providerId": "anthropic", "displayName": "Claude Opus 4.6", "category": "chat", "supportsTools": True, "contextWindow": 200000, "isPlatformDefault": False},
        "claude-sonnet-4-6": {"providerId": "anthropic", "displayName": "Claude Sonnet 4.6", "category": "chat", "supportsTools": True, "contextWindow": 200000, "isPlatformDefault": False},
        "gemini-3.1-pro-preview": {"providerId": "google", "displayName": "Gemini 3.1 Pro Preview", "category": "chat", "supportsTools": True, "contextWindow": 1000000, "isPlatformDefault": False},
        "gemini-3-flash-preview": {"providerId": "google", "displayName": "Gemini 3 Flash Preview", "category": "chat", "supportsTools": True, "contextWindow": 1000000, "isPlatformDefault": False},
        "glm-5": {"providerId": "zai", "displayName": "GLM-5", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "glm-4-plus": {"providerId": "glm", "displayName": "GLM-4-Plus", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "deepseek-chat": {"providerId": "deepseek", "displayName": "DeepSeek Chat", "category": "chat", "supportsTools": True, "contextWindow": 64000, "isPlatformDefault": False},
        "deepseek-reasoner": {"providerId": "deepseek", "displayName": "DeepSeek Reasoner", "category": "reasoning", "supportsTools": True, "contextWindow": 64000, "isPlatformDefault": False},
        "qwen-max": {"providerId": "qwen", "displayName": "Qwen Max", "category": "chat", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "qwen-plus": {"providerId": "qwen", "displayName": "Qwen Plus", "category": "chat", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "qwen-turbo": {"providerId": "qwen", "displayName": "Qwen Turbo", "category": "chat", "supportsTools": True, "contextWindow": 16000, "isPlatformDefault": False},
        "kimi-k2.5": {"providerId": "moonshot", "displayName": "Kimi K2.5", "category": "chat", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "kimi-k2-0905-preview": {"providerId": "moonshot", "displayName": "Kimi K2 0905 Preview", "category": "chat", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "kimi-k2-turbo-preview": {"providerId": "moonshot", "displayName": "Kimi K2 Turbo Preview", "category": "chat", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "kimi-k2-thinking": {"providerId": "moonshot", "displayName": "Kimi K2 Thinking", "category": "reasoning", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "kimi-k2-thinking-turbo": {"providerId": "moonshot", "displayName": "Kimi K2 Thinking Turbo", "category": "reasoning", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "k2p5": {"providerId": "kimi-coding", "displayName": "Kimi Coding K2.5", "category": "coding", "supportsTools": True, "contextWindow": 32000, "isPlatformDefault": False},
        "mistral-large-latest": {"providerId": "mistral", "displayName": "Mistral Large Latest", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "anthropic/claude-opus-4.6": {"providerId": "openrouter", "displayName": "OpenRouter Anthropic Claude Opus 4.6", "category": "chat", "supportsTools": True, "contextWindow": 200000, "isPlatformDefault": False},
        "anthropic/claude-sonnet-4.6": {"providerId": "vercel-ai-gateway", "displayName": "AI Gateway Anthropic Claude Sonnet 4.6", "category": "chat", "supportsTools": True, "contextWindow": 200000, "isPlatformDefault": False},
        "zai-glm-4.7": {"providerId": "cerebras", "displayName": "Cerebras GLM 4.7", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "zai-glm-4.6": {"providerId": "cerebras", "displayName": "Cerebras GLM 4.6", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "deepseek-ai/DeepSeek-R1": {"providerId": "huggingface", "displayName": "DeepSeek R1", "category": "reasoning", "supportsTools": False, "contextWindow": 128000, "isPlatformDefault": False},
        "grok-code-fast-1": {"providerId": "kilocode", "displayName": "Grok Code Fast 1", "category": "coding", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "glm-5-free": {"providerId": "kilocode", "displayName": "GLM-5 Free", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
        "MiniMax-M2.5": {"providerId": "minimax", "displayName": "MiniMax M2.5", "category": "chat", "supportsTools": True, "contextWindow": 128000, "isPlatformDefault": False},
    },
}


def get_openclaw_model_catalog(version: str | None = None) -> OpenClawCatalogSnapshot:
    normalized = (version or DEFAULT_OPENCLAW_VERSION).strip() or DEFAULT_OPENCLAW_VERSION
    return OPENCLAW_MODEL_CATALOGS.get(normalized, OPENCLAW_MODEL_CATALOGS[DEFAULT_OPENCLAW_VERSION])


def normalize_model_source_mode(source_mode: str | None) -> str:
    return "platform"


def provider_allowed(provider_id: str | None, version: str | None = None) -> bool:
    if not provider_id:
        return False
    return provider_id in get_openclaw_model_catalog(version).provider_ids


def model_allowed(model_id: str | None, version: str | None = None) -> bool:
    if not model_id:
        return False
    normalized = normalize_catalog_model_id(model_id, version)
    return normalized in get_openclaw_model_catalog(version).model_ids


def package_allowed(package_id: str | None, version: str | None = None) -> bool:
    if not package_id:
        return False
    catalog = get_openclaw_model_catalog(version)
    return not catalog.package_ids or package_id in catalog.package_ids


def iter_catalog_provider_ids(version: str | None = None) -> Iterable[str]:
    return get_openclaw_model_catalog(version).provider_ids


def iter_catalog_model_ids(version: str | None = None) -> Iterable[str]:
    return get_openclaw_model_catalog(version).model_ids


def iter_catalog_package_ids(version: str | None = None) -> Iterable[str]:
    return get_openclaw_model_catalog(version).package_ids


def get_catalog_provider_metadata(provider_id: str, version: str | None = None) -> dict[str, object]:
    catalog_version = get_openclaw_model_catalog(version).version
    return OPENCLAW_PROVIDER_METADATA.get(catalog_version, {}).get(provider_id, {})


def get_catalog_model_metadata(model_id: str, version: str | None = None) -> dict[str, object]:
    catalog_version = get_openclaw_model_catalog(version).version
    normalized = normalize_catalog_model_id(model_id, version)
    return OPENCLAW_MODEL_METADATA.get(catalog_version, {}).get(normalized, {})


def normalize_catalog_model_id(model_id: str | None, version: str | None = None) -> str:
    normalized = (model_id or "").strip()
    if not normalized:
        return ""
    catalog_version = get_openclaw_model_catalog(version).version
    return OPENCLAW_MODEL_ID_ALIASES.get(catalog_version, {}).get(normalized, normalized)
