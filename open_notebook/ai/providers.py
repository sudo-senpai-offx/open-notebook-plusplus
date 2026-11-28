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

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
import asyncio
import time

from loguru import logger

# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

PROVIDER_DEEPSEEK_WEB = "deepseek-web"
PROVIDER_GOOGLE_AI_MODE = "google-ai-mode"
PROVIDER_DUCK_AI = "duck-ai"
PROVIDER_OLLAMA = "ollama"
PROVIDER_GROQ = "groq"
PROVIDER_NVIDIA = "nvidia"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_SAMBANOVA = "sambanova"

ALL_CLOUD_PROVIDERS = [
    PROVIDER_DEEPSEEK_WEB,
    PROVIDER_GOOGLE_AI_MODE,
    PROVIDER_DUCK_AI,
    PROVIDER_GROQ,
    PROVIDER_NVIDIA,
    PROVIDER_OPENROUTER,
    PROVIDER_SAMBANOVA,
]

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
    # Curated default model whitelist per provider (used for UI default,
    # fallback routing, and smoke tests). Discovery still runs on /v1/models
    # but routing prefers these models.
    default_models: List[str] = field(default_factory=list)
    # Extra HTTP headers merged into every request (e.g. OpenRouter attribution).
    extra_headers: Dict[str, str] = field(default_factory=dict)


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
    PROVIDER_GROQ: ProviderConfig(
        provider_id=PROVIDER_GROQ,
        display_name="Groq",
        supports_attachments=False,
        supports_reasoning=True,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=131_072,
        default_model="openai/gpt-oss-120b",
        default_models=[
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
        ],
        available_models=[],
        rate_limit_requests=25,
        rate_limit_window_seconds=60,
        min_interval_seconds=2.4,
        test_model="openai/gpt-oss-120b",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
        openai_compat_discovery_url="https://api.groq.com/openai/v1/models",
        required_env=("GROQ_API_KEY",),
        base_url_env="GROQ_API_BASE",
    ),
    PROVIDER_NVIDIA: ProviderConfig(
        provider_id=PROVIDER_NVIDIA,
        display_name="NVIDIA NIM",
        supports_attachments=False,
        supports_reasoning=True,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=131_072,
        default_model="meta/llama-3.1-70b-instruct",
        default_models=[
            "meta/llama-3.1-70b-instruct",
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "google/gemma-4-31b-it",
        ],
        available_models=[],
        rate_limit_requests=20,
        rate_limit_window_seconds=60,
        min_interval_seconds=3.0,
        test_model="meta/llama-3.1-70b-instruct",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
        openai_compat_discovery_url="https://integrate.api.nvidia.com/v1/models",
        required_env=("NVIDIA_API_KEY",),
        base_url_env="NVIDIA_API_BASE",
    ),
    PROVIDER_OPENROUTER: ProviderConfig(
        provider_id=PROVIDER_OPENROUTER,
        display_name="OpenRouter",
        supports_attachments=False,
        supports_reasoning=True,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=131_072,
        default_model="openai/gpt-oss-20b",
        default_models=[
            "openai/gpt-oss-20b",
            "google/gemma-4-31b-it:free",
        ],
        available_models=[],
        rate_limit_requests=17,
        rate_limit_window_seconds=60,
        min_interval_seconds=3.5,
        test_model="openai/gpt-oss-20b",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
        openai_compat_discovery_url="https://openrouter.ai/api/v1/models",
        required_env=("OPENROUTER_API_KEY",),
        base_url_env="OPENROUTER_API_BASE",
        extra_headers={
            "HTTP-Referer": "https://github.com/sudo-senpai-offx/open-notebook-plusplus",
            "X-OpenRouter-Title": "Open Notebook++",
            "X-OpenRouter-Categories": "education,productivity",
        },
    ),
    PROVIDER_SAMBANOVA: ProviderConfig(
        provider_id=PROVIDER_SAMBANOVA,
        display_name="SambaNova Cloud",
        supports_attachments=False,
        supports_reasoning=True,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=16_384,
        default_model="Meta-Llama-3.1-8B-Instruct",
        default_models=[
            "Meta-Llama-3.1-8B-Instruct",
            "DeepSeek-R1",
            "gpt-oss-120b",
        ],
        available_models=[],
        rate_limit_requests=17,
        rate_limit_window_seconds=60,
        min_interval_seconds=3.5,
        test_model="Meta-Llama-3.1-8B-Instruct",
        test_model_type="chat",
        modalities=("language",),
        env_config=_default_env_config,
        openai_compat_discovery_url="https://api.sambanova.ai/v1/models",
        required_env=("SAMBANOVA_API_KEY",),
        base_url_env="SAMBANOVA_API_BASE",
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
        retry_after: Optional[float] = None,
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
        self.retry_after = retry_after
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
        # Rate limit tracking: timestamps of requests in the current window
        self._request_times: List[float] = []
        self._lock = asyncio.Lock()

    async def _check_rate_limit(self) -> Optional[str]:
        """Check if a request would exceed the rate limit.

        Returns an error message if rate limited, None otherwise.
        Proactively returns 429 before hitting the provider's actual limit
        to avoid client bans and ToS violations.
        """
        async with self._lock:
            now = time.monotonic()
            # Drop requests outside the window
            self._request_times = [
                t for t in self._request_times
                if now - t < self.config.rate_limit_window_seconds
            ]
            if len(self._request_times) >= self.config.rate_limit_requests:
                return (
                    f"Rate limit exceeded: {self.config.rate_limit_requests} "
                    f"requests per {self.config.rate_limit_window_seconds}s. "
                    f"Wait for quota reset."
                )
            self._request_times.append(now)
        return None

    async def is_available(self) -> bool:
        """Return True if the provider is currently reachable and configured."""
        raise NotImplementedError

    async def _send_message_impl(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        ref_file_ids: Optional[List[str]] = None,
        timeout: float = 120.0,
        model: Optional[str] = None,
    ) -> ProviderSendResult:
        """Send a message and return the result. Implemented by each provider."""
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
        """Send a message with transient-error retry and exponential backoff.

        Retry classification:
          - 429 (rate_limited=True), timeout, connection errors → transient,
            retried with exponential backoff + jitter. Retry-After header
            (provider sets retry_after on ProviderSendResult) is honored.
          - 401, 403, 404, invalid API key → permanent, fail fast.
          - Circuit breaker open → permanent for this call.

        Returns the final ProviderSendResult (ok=True on success, or the
        last error result if all retries are exhausted).
        """
        from open_notebook.utils.retry_classification import should_retry, retry_backoff_seconds

        max_attempts = 3
        attempt = 0

        while True:
            attempt += 1
            result = await self._send_message_impl(
                message,
                session_id=session_id,
                thinking_enabled=thinking_enabled,
                ref_file_ids=ref_file_ids,
                timeout=timeout,
                model=model,
            )

            if result.ok:
                return result

            # Classify the error
            should, delay = should_retry(
                RuntimeError(result.error or "provider error"),
                attempt,
                max_attempts=max_attempts,
                retry_unknown=False,  # unknown errors → don't retry
            )

            if not should:
                return result

            # Honor Retry-After if the provider sent one
            if result.retry_after is not None:
                delay = result.retry_after

            # Cap retry delay — never sleep longer than MAX_RETRY_AFTER_SECONDS.
            # If a provider asks us to wait longer, open the circuit breaker
            # and fall to the next provider instead of hanging the pipeline
            # for an hour (e.g. Retry-After: 3600 on a free tier).
            MAX_RETRY_AFTER_SECONDS = 60.0
            if (
                result.retry_after is not None
                and result.retry_after > MAX_RETRY_AFTER_SECONDS
            ):
                from open_notebook.utils.circuit_breaker import (
                    get_circuit_breaker,
                )

                cb = await get_circuit_breaker(self.provider_id)
                cb._state.state = "open"
                cb._state.last_failure_time = time.monotonic()
                logger.warning(
                    f"Provider {self.provider_id}: Retry-After "
                    f"{result.retry_after:.0f}s exceeds "
                    f"{MAX_RETRY_AFTER_SECONDS:.0f}s cap — opening circuit "
                    f"breaker, falling to next provider"
                )
                return result

            delay = min(delay, MAX_RETRY_AFTER_SECONDS)
            logger.debug(
                f"Provider {self.provider_id}: transient error on attempt "
                f"{attempt}/{max_attempts}, retrying in {delay:.1f}s: "
                f"{result.error}"
            )
            await asyncio.sleep(delay)

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