"""Model routing helpers for low-resource envelopes.

Defaults to mini models (152M-1.5B q4) on low-end; large/VLM opt-in via env.
"""

import os

DEFAULT_MINI_MODELS = {
    "chat": os.getenv("OPEN_NOTEBOOK_MINI_MODEL_CHAT", "mistral-nemo:latest"),
    "embedding": os.getenv("OPEN_NOTEBOOK_MINI_MODEL_EMBED", "nomic-embed-text:latest"),
    "transform": os.getenv(
        "OPEN_NOTEBOOK_MINI_MODEL_TRANSFORM", "llama3.2:3b-instruct-q4_0"
    ),
}

FORCE_MINI = os.getenv("OPEN_NOTEBOOK_FORCE_MINI_MODELS", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def get_routing_hint(task: str) -> dict:
    return {
        "task": task,
        "force_mini": FORCE_MINI,
        "mini_models": DEFAULT_MINI_MODELS,
    }
