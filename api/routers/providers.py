"""
Providers Router

Exposes the provider registry (open_notebook/ai/provider_registry.py) so
clients can enumerate supported providers and their metadata instead of
keeping their own copies.

Endpoints:
- GET /providers - List all supported providers with metadata
- GET /providers/status - Dynamic availability status for all registered providers
"""

from typing import Any, Dict, List

from fastapi import APIRouter

from api.credentials_service import check_env_configured
from api.models import ProviderInfoResponse
from open_notebook.ai.provider_registry import get_provider_registry
from open_notebook.ai.providers import PROVIDERS, PROVIDER_DEEPSEEK_WEB

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=List[ProviderInfoResponse])
async def list_providers():
    """List all supported AI providers with their registry metadata."""
    return [
        ProviderInfoResponse(
            name=spec.provider_id,
            display_name=spec.display_name,
            modalities=list(spec.modalities),
            docs_url=None,
            env_configured=check_env_configured(spec.provider_id),
        )
        for spec in PROVIDERS.values()
    ]


@router.get("/status", response_model=List[Dict[str, Any]])
async def provider_status():
    """Return dynamic availability status for all registered providers.

    Calls each provider's is_available() to determine if it can currently
    handle requests. Includes detailed per-provider diagnostics.
    """
    registry = get_provider_registry()
    results: List[Dict[str, Any]] = []

    for provider_id in [PROVIDER_DEEPSEEK_WEB, "google-ai-mode", "duck-ai", "ollama"]:
        provider = registry.get(provider_id)
        if provider is None:
            results.append({
                "provider_id": provider_id,
                "available": False,
                "error": "Provider not registered",
            })
            continue

        try:
            available = await provider.is_available()
        except Exception as exc:  # noqa: BLE001
            available = False
            error = str(exc)
        else:
            error = None

        result: Dict[str, Any] = {
            "provider_id": provider_id,
            "available": available,
        }

        # Add provider-specific diagnostics
        if provider_id == PROVIDER_DEEPSEEK_WEB:
            from open_notebook.ai.models import ProcessingFlags
            flags = await ProcessingFlags.get_instance()
            from open_notebook.services.deepseek_direct_service import pow_solver_available
            result["diagnostics"] = {
                "has_session_id": bool(flags.deepseek_ds_session_id),
                "has_authorization_token": bool(flags.deepseek_authorization_token),
                "pow_solver_available": pow_solver_available(),
            }
        elif provider_id == "google-ai-mode":
            from open_notebook.ai.models import ProcessingFlags
            flags = await ProcessingFlags.get_instance()
            result["diagnostics"] = {
                "has_wrapper_url": bool(flags.google_ai_mode_url),
                "has_api_key": bool(flags.gemini_api_key),
            }
        elif provider_id == "duck-ai":
            import importlib.util
            p2d_available = (
                importlib.util.find_spec("p2d_duck") is not None
                or importlib.util.find_spec("duck_ai") is not None
            )
            result["diagnostics"] = {
                "p2d_duck_installed": p2d_available,
            }
            from open_notebook.ai.models import ProcessingFlags
            flags = await ProcessingFlags.get_instance()
            result["diagnostics"]["has_duck_ai_url"] = bool(flags.duck_ai_url)

        if error:
            result["error"] = error

        results.append(result)

    return results
