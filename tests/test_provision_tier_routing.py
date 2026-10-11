"""T1 correction: the transform mini default (3B) must not route to a 2GB
tier. When the tier denies LoRA-class memory, transform falls back to the
chat mini; big tiers keep the transform mini. Explicit model choices are
untouched (FORCE_MINI off path not covered here).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from esperanto import LanguageModel

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.utils.model_router import DEFAULT_MINI_MODELS


def _model():
    return MagicMock(spec=LanguageModel)


@pytest.mark.asyncio
async def test_transform_falls_back_to_chat_mini_on_low_tier():
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(False, "denied"),
        ),
        patch("open_notebook.ai.provision.model_manager") as manager,
    ):
        manager.get_model = AsyncMock(side_effect=lambda i, **k: _model())
        manager.get_default_model = AsyncMock(return_value=_model())
        await provision_langchain_model("content", None, "transform")
        manager.get_model.assert_called_with(
            DEFAULT_MINI_MODELS["chat"],
        )


@pytest.mark.asyncio
async def test_transform_keeps_transform_mini_on_big_tier():
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(True, "open"),
        ),
        patch("open_notebook.ai.provision.model_manager") as manager,
    ):
        manager.get_model = AsyncMock(side_effect=lambda i, **k: _model())
        manager.get_default_model = AsyncMock(return_value=_model())
        await provision_langchain_model("content", None, "transform")
        manager.get_model.assert_called_with(
            DEFAULT_MINI_MODELS["transform"],
        )


@pytest.mark.asyncio
async def test_non_transform_types_unaffected_by_tier():
    with (
        patch("open_notebook.utils.model_router.FORCE_MINI", True),
        patch(
            "open_notebook.utils.capability_tier.tier_allows",
            return_value=(False, "denied"),
        ),
        patch("open_notebook.ai.provision.model_manager") as manager,
    ):
        manager.get_model = AsyncMock(side_effect=lambda i, **k: _model())
        manager.get_default_model = AsyncMock(return_value=_model())
        await provision_langchain_model("content", None, "chat")
        manager.get_model.assert_called_with(
            DEFAULT_MINI_MODELS["chat"],
        )
