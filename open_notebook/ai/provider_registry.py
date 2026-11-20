"""
Provider registry initialization.

Reads provider configuration from ProcessingFlags (SurrealDB) and registers
provider instances with the global ProviderRegistry.

Called at application startup and whenever provider configuration changes.
"""

from __future__ import annotations

from loguru import logger

from open_notebook.ai.providers import (
    PROVIDER_DEEPSEEK_WEB,
    PROVIDER_DUCK_AI,
    PROVIDER_GOOGLE_AI_MODE,
    PROVIDER_OLLAMA,
    PROVIDER_METADATA,
    registry,
)
from open_notebook.services.deepseek_web_provider import DeepSeekWebProvider
from open_notebook.services.google_ai_mode_provider import GoogleAIModeProvider
from open_notebook.services.duck_ai_provider import DuckAIProvider


async def initialize_providers() -> None:
    """Read ProcessingFlags and register all configured providers."""
    # Import ProcessingFlags lazily to avoid circular import with
    # open_notebook.ai.connection_tester -> provider_registry -> models
    from open_notebook.ai.models import ProcessingFlags

    flags = await ProcessingFlags.get_instance()

    # --- DeepSeek-web ---
    deepseek_config = PROVIDER_METADATA[PROVIDER_DEEPSEEK_WEB]
    registry.register(DeepSeekWebProvider(deepseek_config))
    logger.info("Registered provider: deepseek-web")

    # --- Google AI Mode ---
    google_config = PROVIDER_METADATA[PROVIDER_GOOGLE_AI_MODE]
    registry.register(GoogleAIModeProvider(google_config))
    logger.info("Registered provider: google-ai-mode")

    # --- Duck.ai ---
    duck_config = PROVIDER_METADATA[PROVIDER_DUCK_AI]
    # Override default model from flags if configured
    if flags.duck_ai_default_model:
        from dataclasses import replace

        duck_config = replace(duck_config, default_model=flags.duck_ai_default_model)
    registry.register(DuckAIProvider(duck_config))
    logger.info("Registered provider: duck-ai")

    # --- Ollama (local) ---
    ollama_config = PROVIDER_METADATA[PROVIDER_OLLAMA]
    registry.register(
        type(
            "OllamaProvider",
            (object,),
            {
                "provider_id": PROVIDER_OLLAMA,
                "config": ollama_config,
                "is_available": lambda self: True,
                "send_message": lambda self, **kwargs: _ollama_placeholder(),
                "upload_attachment": lambda self, path: None,
                "delete_session": lambda self, sid: True,
                "get_credentials": lambda self: {"provider": PROVIDER_OLLAMA},
            },
        )(ollama_config)
    )
    logger.info("Registered provider: ollama (local placeholder)")


async def _ollama_placeholder():
    """Placeholder for Ollama provider — not yet implemented."""
    from open_notebook.ai.providers import ProviderSendResult

    return ProviderSendResult(
        ok=False,
        error="Ollama provider not yet implemented in the new provider abstraction.",
        used_provider="ollama",
    )


def get_provider_registry():
    """Return the global provider registry."""
    return registry