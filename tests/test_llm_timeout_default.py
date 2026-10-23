"""Open Notebook defaults ESPERANTO_LLM_TIMEOUT to something its own hardware
can finish (#1434).

esperanto 2.28 enforces the timeout in to_langchain() on every provider, with
a 60 s default that cuts off long answers and slow local models. Open
Notebook used to default it to 180 s, which is comfortable for a cloud API
answering a chat reply and three times too short for the hardware this
supports - so every part of every long document failed, and the failure said
the provider could not be reached.
"""

import pytest
from esperanto import AIFactory

from open_notebook.config import DEFAULT_LLM_TIMEOUT_SECONDS, ensure_llm_timeout_default

# One part of a long document, measured on the floor this supports: a phone,
# qwen2.5-1.5b at two threads with a 4,096-token window. A 2,221-token prompt
# took 264 s to evaluate and 250 tokens took 152 s to write - 417 s for one
# request. The slowest hardware named in this project's floor is slower than
# that phone, and the reduce step that follows is longer still.
MEASURED_PART_SECONDS = 417.0


def test_the_default_covers_a_part_of_a_long_document():
    """A part has to be able to finish, or the document never gets analysed."""
    assert DEFAULT_LLM_TIMEOUT_SECONDS > MEASURED_PART_SECONDS


def test_default_applies_when_unset():
    env: dict[str, str] = {}
    ensure_llm_timeout_default(env)
    assert env["ESPERANTO_LLM_TIMEOUT"] == str(DEFAULT_LLM_TIMEOUT_SECONDS)


@pytest.mark.parametrize("value", ["", "   "])
def test_default_applies_when_blank(value):
    env = {"ESPERANTO_LLM_TIMEOUT": value}
    ensure_llm_timeout_default(env)
    assert env["ESPERANTO_LLM_TIMEOUT"] == str(DEFAULT_LLM_TIMEOUT_SECONDS)


def test_explicit_value_wins():
    env = {"ESPERANTO_LLM_TIMEOUT": "90"}
    ensure_llm_timeout_default(env)
    assert env["ESPERANTO_LLM_TIMEOUT"] == "90"


@pytest.mark.parametrize(
    "env_value,expected",
    [(None, float(DEFAULT_LLM_TIMEOUT_SECONDS)), ("90", 90.0)],
)
def test_langchain_model_carries_the_timeout(monkeypatch, env_value, expected):
    monkeypatch.delenv("ESPERANTO_LLM_TIMEOUT", raising=False)
    if env_value is not None:
        monkeypatch.setenv("ESPERANTO_LLM_TIMEOUT", env_value)
    import os

    ensure_llm_timeout_default(os.environ)

    model = AIFactory.create_language(
        "anthropic", "claude-sonnet-5", config={"api_key": "test"}
    ).to_langchain()

    assert float(model.default_request_timeout) == expected
