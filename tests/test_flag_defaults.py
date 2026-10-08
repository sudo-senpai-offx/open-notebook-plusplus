"""C2: model-policy flags must stay OFF (offline-first) when the env says nothing.

Cloud access, the router, mixture retrieval and the reranker are all opt-in.
That is a product promise, not an accident of the current defaults -- a future
edit must not be able to quietly flip one on. This test pins the defaults so
governance is enforced in code rather than left to convention or documentation.

The env is scrubbed before every test so an ambient shell/CI variable can never
make the "defaults are off" assertions pass vacuously.
"""

from __future__ import annotations

import pytest

from open_notebook.ai import model_policy
from open_notebook.utils.hybrid_search import hybrid_enabled

# Every flag read by get_policy_config().
_POLICY_FLAGS = (
    "AUTO_MODEL_POLICY",
    "CLOUD_ALLOWED",
    "PREFER_LOCAL",
    "DEGRADE_GRACEFULLY",
    "TIME_BUDGETS_ENFORCED",
    "USE_ROUTER",
    "USE_MIXTURE",
    "USE_RERANKER_BEFORE_TRUNCATE",
)

# The flags that must default to FALSE (the "never auto-enable" set).
_OPT_IN_FLAGS = (
    "CLOUD_ALLOWED",
    "USE_ROUTER",
    "USE_MIXTURE",
    "USE_RERANKER_BEFORE_TRUNCATE",
)


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    for flag in _POLICY_FLAGS:
        monkeypatch.delenv(flag, raising=False)


def test_defaults_are_offline_first_with_env_unset():
    assert model_policy.get_policy_config() == {
        "AUTO_MODEL_POLICY": "offline-first",
        "CLOUD_ALLOWED": False,
        "PREFER_LOCAL": True,
        "DEGRADE_GRACEFULLY": True,
        "TIME_BUDGETS_ENFORCED": True,
        "USE_ROUTER": False,
        "USE_MIXTURE": False,
        "USE_RERANKER_BEFORE_TRUNCATE": False,
    }


@pytest.mark.parametrize("flag", _OPT_IN_FLAGS)
def test_opt_in_flags_default_false(flag):
    assert model_policy.get_policy_config()[flag] is False


def test_accessors_default_off():
    assert model_policy.allow_cloud() is False
    assert model_policy.use_router() is False
    assert model_policy.use_mixture() is False
    assert model_policy.use_reranker() is False


def test_hybrid_search_gated_by_use_mixture_off_by_default():
    # hybrid_enabled() delegates to use_mixture(); there is no separate flag.
    # A "hybrid flag" therefore cannot be added without this test moving too.
    assert hybrid_enabled() is False


@pytest.mark.parametrize("flag", _OPT_IN_FLAGS)
def test_opt_in_flags_flip_on_when_set(monkeypatch, flag):
    # Companion to the default test: proves the parser actually reads the flag,
    # so the "default false" assertions above are not passing because the
    # parser is broken/ignoring the environment.
    monkeypatch.setenv(flag, "true")
    assert model_policy.get_policy_config()[flag] is True


def test_hybrid_enabled_follows_use_mixture(monkeypatch):
    monkeypatch.setenv("USE_MIXTURE", "1")
    assert hybrid_enabled() is True
