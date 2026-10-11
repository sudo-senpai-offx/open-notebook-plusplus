from esperanto import LanguageModel
from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from open_notebook.ai.models import model_manager
from open_notebook.exceptions import ConfigurationError
from open_notebook.utils import token_count


async def provision_langchain_model(
    content, model_id, default_type, **kwargs
) -> BaseChatModel:
    """
    Returns the best model to use based on the context size and on whether there is a specific model being requested in Config.
    If context > 105_000, returns the large_context_model
    If model_id is specified in Config, returns that model
    Otherwise, returns the default model for the given type
    """
    tokens = token_count(content)
    model = None
    selection_reason = ""

    from open_notebook.utils.model_router import DEFAULT_MINI_MODELS, FORCE_MINI

    if tokens > 105_000 and not FORCE_MINI:
        selection_reason = f"large_context (content has {tokens} tokens)"
        logger.debug(
            f"Using large context model because the content has {tokens} tokens"
        )
        model = await model_manager.get_default_model("large_context", **kwargs)
    elif model_id and not FORCE_MINI:
        selection_reason = f"explicit model_id={model_id}"
        model = await model_manager.get_model(model_id, **kwargs)
    else:
        selection_reason = f"default for type={default_type}"
        model = await model_manager.get_default_model(default_type, **kwargs)
        if FORCE_MINI and default_type in DEFAULT_MINI_MODELS:
            try:
                mini_id = DEFAULT_MINI_MODELS[default_type]
                if mini_id:
                    # T1 correction: the transform mini (3B) does not fit the
                    # 2GB tier, so route transform to the chat mini there.
                    # Big tiers keep the transform mini.
                    if default_type == "transform":
                        from open_notebook.utils.capability_tier import tier_allows

                        allowed, reason = tier_allows("lora")
                        if not allowed:
                            logger.debug(
                                "Tier denies 3B transform model "
                                f"({reason}); falling back to chat mini."
                            )
                            mini_id = DEFAULT_MINI_MODELS["chat"]
                    mini_model = await model_manager.get_model(mini_id, **kwargs)
                    if mini_model:
                        model = mini_model
                        selection_reason = f"forced_mini for type={default_type}"
            except Exception:
                pass

    logger.debug(f"Using model: {model}")

    if model is None:
        logger.error(
            f"Model provisioning failed: No model found. "
            f"Selection reason: {selection_reason}. "
            f"model_id={model_id}, default_type={default_type}. "
            f"Please check Manage → Models and ensure a default model is configured for '{default_type}'."
        )
        raise ConfigurationError(
            f"No model configured for {selection_reason}. "
            f"Please go to Manage → Models and configure a default model for '{default_type}'."
        )

    if not isinstance(model, LanguageModel):
        logger.error(
            f"Model type mismatch: Expected LanguageModel but got {type(model).__name__}. "
            f"Selection reason: {selection_reason}. "
            f"model_id={model_id}, default_type={default_type}."
        )
        raise ConfigurationError(
            f"Model is not a LanguageModel: {model}. "
            f"Please check that the model configured for '{default_type}' is a language model, not an embedding or speech model."
        )

    return model.to_langchain()
