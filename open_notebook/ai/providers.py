"""
Provider abstraction layer for ON++ cloud AI services.

Separates the concept of a *provider* (DeepSeek-web, Google AI Mode, Duck.ai,
Ollama/local) from a *model* (deepseek-chat, gemini-4-hydrogen, gpt-6-luna,
llama3.2:3b). Each provider has its own credentials, rate limits, attachment
limits, and context window, and knows how to send messages to its upstream API.

Design goals:
- No Playwright/CDP — lightweight wrappers only (curl_cffi, httpx, p2d-duck)
- Provider-agnostic chat routing: chat.py uses ProviderRegistry to dispatch
- Per-provider attachment limits and context windows respected by preprocessor
- Resumable switching: context can be fed to any provider via preprocessing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

PROVIDER_DEEPSEEK_WEB = "deepseek-web"
PROVIDER_GOOGLE_AI_MODE = "google-ai-mode"
PROVIDER_DUCK_AI = "duck-ai"
PROVIDER_OLLAMA = "ollama"

ALL_CLOUD_PROVIDERS = [PROVIDER_DEEPSEEK_WEB, PROVIDER_GOOGLE_AI_MODE, PROVIDER_DUCK_AI]

DEFAULT_DUCK_AI_MODELS = [
    "gpt-6-luna",
    "claude-haiku-5.5",
    "gpt-5.4-mini",
    "mistral-small-4",
    "gpt-oss-120b",
    "gemma-4-31b",
]

DEFAULT_DUCK_AI_MODEL = "gpt-6-luna"


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable metadata for a cloud AI provider."""

    provider_id: str
    display_name: str
    supports_attachments: bool
    supports_reasoning: bool
    max_files: int
    max_size_mb: int
    context_window_tokens: int
    default_model: Optional[str]
    available_models: List[str]
    # Rate limits (requests per window)
    rate_limit_requests: int
    rate_limit_window_seconds: int
    min_interval_seconds: float
    # Test model for connection testing (model_name, model_type)
    test_model: Optional[str] = None
    test_model_type: Optional[str] = None
    # OpenAI-compatible discovery URL (for providers that expose one)
    openai_compat_discovery_url: Optional[str] = None
    # Required environment variable for API key
    required_env: Tuple[str, ...] = ()
    # Base URL environment variable override
    base_url_env: Optional[str] = None
    # Supported modalities (language, embedding, text_to_speech, speech_to_text)
    modalities: Tuple[str, ...] = ("language",)
    # Environment configuration for migration checks
    # Returns dict with keys: "required", "required_any", "optional"
    env_config: Optional[Callable[[], Dict[str, List[str]]]] = None


def _default_env_config() -> Dict[str, List[str]]:
    """Default env config: providers get config from DB, not env vars."""
    return {"required": [], "required_any": [], "optional": []}


PROVIDER_METADATA: Dict[str, ProviderConfig] = {
    PROVIDER_DEEPSEEK_WEB: ProviderConfig(
        provider_id=PROVIDER_DEEPSEEK_WEB,
        display_name="DeepSeek-web",
        supports_attachments=True,
        supports_reasoning=True,
        max_files=50,
        max_size_mb=100,
        context_window_tokens=1_000_000,
        default_model="deepseek-chat",
        available_models=["deepseek-chat"],
        rate_limit_requests=60,
        rate_limit_window_seconds=60,
        min_interval_seconds=1.0,
        test_model="deepseek-chat",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
    ),
    PROVIDER_GOOGLE_AI_MODE: ProviderConfig(
        provider_id=PROVIDER_GOOGLE_AI_MODE,
        display_name="Google AI Mode (Gemini 4 Hydrogen)",
        supports_attachments=True,
        supports_reasoning=True,
        max_files=10,
        max_size_mb=100,
        context_window_tokens=32_000,  # Free tier; sliding window
        default_model="gemini-4-hydrogen",
        available_models=["gemini-4-hydrogen"],
        rate_limit_requests=60,
        rate_limit_window_seconds=60,
        min_interval_seconds=2.0,
        test_model="gemini-4-hydrogen",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
        # Lightpanda CDP server URL for browser-based Google AI Mode access.
        # Run: lightpanda serve --host 127.0.0.1 --port 9222
        base_url_env="LIGHTPANDA_CDP_URL",
    ),
    PROVIDER_DUCK_AI: ProviderConfig(
        provider_id=PROVIDER_DUCK_AI,
        display_name="Duck.ai",
        supports_attachments=True,
        supports_reasoning=False,
        max_files=1,
        max_size_mb=100,
        context_window_tokens=32_000,  # Unpublished; estimated
        default_model=DEFAULT_DUCK_AI_MODEL,
        available_models=list(DEFAULT_DUCK_AI_MODELS),
        rate_limit_requests=20,
        rate_limit_window_seconds=60,
        min_interval_seconds=1.0,
        test_model="gpt-6-luna",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
    ),
    PROVIDER_OLLAMA: ProviderConfig(
        provider_id=PROVIDER_OLLAMA,
        display_name="Ollama (Local)",
        supports_attachments=False,
        supports_reasoning=False,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=8192,
        default_model="llama3.2:3b",
        available_models=[],
        rate_limit_requests=0,
        rate_limit_window_seconds=60,
        min_interval_seconds=0.0,
        test_model="llama3.2:3b",
        test_model_type="chat",
        modalities=("language", "embedding"),
        env_config=_default_env_config,
    ),
}


# PROVIDERS dict exported for connection_tester.py (avoids circular import)
PROVIDERS: Dict[str, ProviderConfig] = dict(PROVIDER_METADATA)


def get_provider_config(provider_id: str) -> Optional[ProviderConfig]:
    """Return metadata for a provider, or None if unknown."""
    return PROVIDER_METADATA.get(provider_id)


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------


class ProviderSendResult:
    """Outcome of sending a message through a provider."""

    def __init__(
        self,
        *,
        ok: bool,
        response: Optional[str] = None,
        thinking: Optional[str] = None,
        error: Optional[str] = None,
        manual_login_required: bool = False,
        rate_limited: bool = False,
        session_id: Optional[str] = None,
        used_provider: Optional[str] = None,
        ref_file_ids: Optional[List[str]] = None,
    ):
        self.ok = ok
        self.response = response
        self.thinking = thinking
        self.error = error
        self.manual_login_required = manual_login_required
        self.rate_limited = rate_limited
        self.session_id = session_id
        self.used_provider = used_provider
        self.ref_file_ids = ref_file_ids or []


class BaseProvider:
    """Abstract base class for cloud AI providers."""

    provider_id: str
    config: ProviderConfig

    def __init__(self, provider_id: str, config: ProviderConfig):
        self.provider_id = provider_id
        self.config = config

    async def is_available(self) -> bool:
        """Return True if the provider is currently reachable and configured."""
        raise NotImplementedError

    async def send_message(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        ref_file_ids: Optional[List[str]] = None,
        timeout: float = 120.0,
        model: Optional[str] = None,
    ) -> ProviderSendResult:
        """Send a message and return the result."""
        raise NotImplementedError

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Upload a file and return a provider-specific file reference ID."""
        raise NotImplementedError

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session on the provider side (best effort)."""
        raise NotImplementedError

    def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """Singleton registry mapping provider_id -> provider instance."""

    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        return self._providers.get(provider_id)

    def get_all(self) -> Dict[str, BaseProvider]:
        return dict(self._providers)

    def is_available(self, provider_id: str) -> bool:
        provider = self._providers.get(provider_id)
        if provider is None:
            return False
        return provider.is_available()


registry = ProviderRegistry()


def register_provider(provider: BaseProvider) -> None:
    """Convenience function to register a provider with the global registry."""
    registry.register(provider)


def get_provider(provider_id: str) -> Optional[BaseProvider]:
    """Convenience function to get a provider from the global registry."""
    return registry.get(provider_id)