"""
API Key Provider - Database-first with environment fallback.

This module provides a unified interface for retrieving API keys and provider
configuration. It reads from Credential records (individual per-provider
credentials) and falls back to environment variables for backward compatibility.

Usage:
    from open_notebook.ai.key_provider import provision_provider_keys

    # Call before model provisioning to set env vars from DB
    await provision_provider_keys("openai")
"""

import os
from typing import Dict, Optional

from loguru import logger

from open_notebook.ai.provider_registry import PROVIDERS
from open_notebook.domain.credential import Credential

# =============================================================================
# Provider Configuration Mapping
# =============================================================================
# Maps provider names to their environment variable names.
# This is the single source of truth for provider-to-env-var mapping.
#
# Only local/self-hosted providers remain. All cloud API key providers
# (OpenAI, Anthropic, DeepSeek API, etc.) have been unwired — ON++ now uses
# the free DeepSeek-web tier (session cookies, not API keys) and local
# models via Ollama/oMLX.

PROVIDER_CONFIG: Dict[str, dict] = {
    # URL-based local providers (no API key required, base URL only)
    "ollama": {
        "env_var": "OLLAMA_API_BASE",
    },
    # oMLX: Esperanto built-in profile (OMLX_API_KEY / OMLX_API_BASE).
    # Optional API key; base_url from the credential sets OMLX_API_BASE via
    # _provision_simple_provider. No OPENAI_COMPATIBLE_* mirroring.
    "omlx": {
        "env_var": "OMLX_API_KEY",
    },
}


async def _get_default_credential(provider: str) -> Optional[Credential]:
    """Get the first credential for a provider from the database."""
    try:
        credentials = await Credential.get_by_provider(provider)
        if credentials:
            return credentials[0]
    except Exception as e:
        logger.debug(f"Could not load credential from database for {provider}: {e}")
    return None


async def get_api_key(provider: str) -> Optional[str]:
    """
    Get API key for a provider. Checks database first, then env var.

    Args:
        provider: Provider name (openai, anthropic, etc.)

    Returns:
        API key string or None if not configured
    """
    cred = await _get_default_credential(provider)
    if cred and cred.api_key:
        logger.debug(f"Using {provider} API key from Credential")
        return cred.api_key.get_secret_value()

    # Fall back to environment variable
    config_info = PROVIDER_CONFIG.get(provider.lower())
    if config_info:
        env_value = os.environ.get(config_info["env_var"])
        if env_value:
            logger.debug(f"Using {provider} API key from environment variable")
        return env_value

    return None


async def _provision_simple_provider(provider: str) -> bool:
    """
    Set environment variable for a simple provider from DB config.

    Returns:
        True if key was set from database, False otherwise
    """
    provider_lower = provider.lower()
    config_info = PROVIDER_CONFIG.get(provider_lower)
    if not config_info:
        return False

    env_var = config_info["env_var"]

    cred = await _get_default_credential(provider_lower)
    if not cred:
        return False

    # Set API key / primary env var
    if cred.api_key:
        os.environ[env_var] = cred.api_key.get_secret_value()
        logger.debug(f"Set {env_var} from Credential")

    # Set base URL if present
    if cred.base_url:
        provider_upper = provider_lower.upper()
        os.environ[f"{provider_upper}_API_BASE"] = cred.base_url
        logger.debug(f"Set {provider_upper}_API_BASE from Credential")
        # Providers with a registry-declared endpoint override (ADR-012) read
        # it under that name (esperanto's profiles and env discovery).
        spec = PROVIDERS.get(provider_lower)
        if spec and spec.base_url_env:
            os.environ[spec.base_url_env] = cred.base_url
            logger.debug(f"Set {spec.base_url_env} from Credential")

    return True


async def provision_provider_keys(provider: str) -> bool:
    """
    Provision environment variables from database for a specific provider.

    Only local/self-hosted providers remain (ollama, omlx). All cloud API key
    providers have been unwired — ON++ uses the free DeepSeek-web tier
    (session cookies, not API keys) and local models via Ollama/oMLX.

    Args:
        provider: Provider name (ollama, omlx)

    Returns:
        True if any keys were set from database, False otherwise
    """
    # Normalize provider name
    provider_lower = provider.lower()

    # Handle simple providers
    return await _provision_simple_provider(provider_lower)


async def provision_all_keys() -> dict[str, bool]:
    """
    Provision environment variables from database for all providers.

    NOTE: This function is deprecated for request-time use because it can leave
    stale env vars after key deletion. Keys should only be provisioned at startup
    or via provision_provider_keys() for specific providers.

    Useful at application startup to load all DB-stored keys into environment.

    Returns:
        Dict mapping provider names to whether keys were set from DB
    """
    results: dict[str, bool] = {}

    # Simple providers (only local providers remain in PROVIDER_CONFIG)
    for provider in PROVIDER_CONFIG.keys():
        results[provider] = await provision_provider_keys(provider)

    return results
