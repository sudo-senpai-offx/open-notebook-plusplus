"""Routing hook: sub-3GB tiers may offload chat/transform inference to the
opt-in no-AVX sidecar (OpenAI-compatible /v1). Host Ollama stays the path
when the flag/URL are unset or the tier fits local 3B.
"""

from unittest.mock import MagicMock, patch

import pytest
from esperanto import LanguageModel

from open_notebook.ai.provision import provision_langchain_model

SIDECAR_URL = "http://127.0.0.1:8099/v1"


def _model():
    return MagicMock(spec=LanguageModel)


def _env(monkeypatch, allow="true", url=SIDECAR_URL):
    monkeypatch.setenv("OPEN_NOTEBOOK_ALLOW_LLAMACPP_SSE2", allow)
    if url is None:
        monkeypatch.delenv("LLAMACPP_SSE2_URL", raising=False)
    else:
        monkeypatch.setenv("LLAMACPP_SSE2_URL", url)


@pytest.mark.asyncio
async def test_low_tier_routes_to_sidecar(monkeypatch):
    _env(monkeypatch)
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(False, "denied"),
        ),
        patch("open_notebook.ai.provision.model_manager"),
        patch("esperanto.AIFactory.create_language", return_value=_model()) as create,
    ):
        await provision_langchain_model("content", None, "transform")
    _, kwargs = create.call_args
    assert kwargs["provider"] == "openai-compatible"
    assert kwargs["config"]["base_url"] == SIDECAR_URL


@pytest.mark.asyncio
async def test_flag_off_keeps_host_path(monkeypatch):
    _env(monkeypatch, allow="false")
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(False, "denied"),
        ),
        patch("open_notebook.ai.provision.model_manager") as manager,
        patch(
            "esperanto.AIFactory.create_language",
            side_effect=AssertionError("must not route"),
        ),
    ):
        from unittest.mock import AsyncMock

        manager.get_model = AsyncMock(side_effect=lambda i, **k: _model())
        manager.get_default_model = AsyncMock(return_value=_model())
        await provision_langchain_model("content", None, "transform")


@pytest.mark.asyncio
async def test_big_tier_keeps_host_path(monkeypatch):
    _env(monkeypatch)
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(True, "open"),
        ),
        patch("open_notebook.ai.provision.model_manager") as manager,
        patch(
            "esperanto.AIFactory.create_language",
            side_effect=AssertionError("must not route"),
        ),
    ):
        from unittest.mock import AsyncMock

        manager.get_model = AsyncMock(side_effect=lambda i, **k: _model())
        manager.get_default_model = AsyncMock(return_value=_model())
        await provision_langchain_model("content", None, "chat")
