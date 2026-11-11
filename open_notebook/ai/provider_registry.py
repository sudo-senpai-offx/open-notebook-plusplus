"""
Provider Registry — the single source of truth for AI provider metadata.

Adding a provider used to require keeping ~6 independent dicts in sync
(env config, modalities, test models, discovery table, API Literal,
frontend table). Now the backend surfaces are all derived from the
`PROVIDERS` registry below:

- `api/credentials_service.py` `PROVIDER_ENV_CONFIG` / `PROVIDER_MODALITIES`
- `open_notebook/ai/connection_tester.py` `TEST_MODELS`
- `open_notebook/ai/model_discovery.py` `OPENAI_COMPAT_PROVIDERS`
- `GET /api/providers` (api/routers/providers.py)

One place still needs a manual edit when adding a provider — enforced by
tests (tests/test_credential_provider_validation.py): the
`SupportedProvider` Literal in `api/models.py` (typing can't be built at
runtime from this dict). The frontend consumes `GET /api/providers` at
runtime, so it needs no edit.

The declaration order below is the display order the frontend renders —
`GET /api/providers` returns `PROVIDERS.values()` as declared.

This module is pure data: it must not import anything from the rest of
the project so it stays importable from anywhere without cycles.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the backend needs to know about one AI provider."""

    name: str
    display_name: str
    # Default modalities offered when creating a credential for this provider.
    modalities: Tuple[str, ...]
    # Env var configuration for env-based setup/migration:
    # - required_env: ALL must be set for the provider to count as configured.
    # - required_any_env: at least ONE must be set.
    # - optional_env: read during migration but not required.
    required_env: Tuple[str, ...] = ()
    required_any_env: Tuple[str, ...] = ()
    optional_env: Tuple[str, ...] = ()
    # Cheapest model used by the provider connection test. None means the
    # test is dynamic (first available model) or handled by a bespoke tester.
    test_model: Optional[str] = None
    test_model_type: str = "language"
    # Where users get an API key / set the provider up.
    docs_url: Optional[str] = None
    # For providers exposing an OpenAI-compatible GET /models endpoint,
    # the discovery URL. Drives OPENAI_COMPAT_PROVIDERS in model_discovery.
    openai_compat_discovery_url: Optional[str] = None

    @property
    def base_url_env(self) -> Optional[str]:
        """Optional env var overriding the provider's endpoint (`*_BASE_URL`).

        Declared through `optional_env`; providers that have one accept a
        user-supplied base URL for discovery (e.g. regional endpoints).
        """
        return next((v for v in self.optional_env if v.endswith("_BASE_URL")), None)

    def env_config(self) -> Dict[str, List[str]]:
        """Env var config in the legacy PROVIDER_ENV_CONFIG dict shape."""
        config: Dict[str, List[str]] = {}
        if self.required_env:
            config["required"] = list(self.required_env)
        if self.required_any_env:
            config["required_any"] = list(self.required_any_env)
        if self.optional_env:
            config["optional"] = list(self.optional_env)
        return config


_LANGUAGE_ONLY = ("language",)
_ALL_MODALITIES = ("language", "embedding", "speech_to_text", "text_to_speech")


_PROVIDER_SPECS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="ollama",
        display_name="Ollama",
        modalities=("language", "embedding"),
        required_env=("OLLAMA_API_BASE",),
        test_model=None,  # Dynamic - uses first available model
    ),
    ProviderSpec(
        name="omlx",
        display_name="oMLX",
        modalities=("language", "embedding"),
        required_env=("OMLX_API_BASE",),
        optional_env=("OMLX_API_KEY",),
        test_model=None,  # Dynamic - uses first available model via /v1/models
        docs_url="https://github.com/lfnovo/open-notebook/blob/main/docs/5-CONFIGURATION/omlx.md",
        # No openai_compat_discovery_url: base URL is user-supplied (default
        # http://localhost:11435/v1) and API key is optional — discovery is
        # handled like openai_compatible, not the fixed-URL OPENAI_COMPAT table.
    ),
)


def _build_registry(specs: Tuple[ProviderSpec, ...]) -> Dict[str, ProviderSpec]:
    """Build the name -> spec map, refusing duplicate names at import time.

    A plain dict comprehension would silently drop the earlier spec on a
    name collision; fail loudly instead.
    """
    registry: Dict[str, ProviderSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise ValueError(f"Duplicate provider name in registry: {spec.name!r}")
        registry[spec.name] = spec
    return registry


PROVIDERS: Dict[str, ProviderSpec] = _build_registry(_PROVIDER_SPECS)
