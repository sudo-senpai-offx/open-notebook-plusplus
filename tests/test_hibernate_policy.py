"""T3: hibernate policy is inert config until a governor reads it.

All three flags default to safe values (disabled / 10min idle / media
stoppable); nothing in the runtime consults them yet.
"""

from __future__ import annotations

import pytest

from open_notebook.ai import model_policy

_FLAGS = (
    "OPEN_NOTEBOOK_HIBERNATE_ENABLE",
    "OPEN_NOTEBOOK_HIBERNATE_IDLE_SECONDS",
    "OPEN_NOTEBOOK_HIBERNATE_STOP_MEDIA",
)


@pytest.fixture(autouse=True)
def _clean_hibernate_env(monkeypatch):
    for flag in _FLAGS:
        monkeypatch.delenv(flag, raising=False)


def test_hibernate_policy_defaults():
    config = model_policy.get_policy_config()
    assert config["HIBERNATE_ENABLE"] is False
    assert config["HIBERNATE_IDLE_SECONDS"] == 600
    assert config["HIBERNATE_STOP_MEDIA"] is True
    assert model_policy.hibernate_enabled() is False
    assert model_policy.hibernate_idle_seconds() == 600
    assert model_policy.hibernate_stop_media() is True


def test_hibernate_enable_flips_on(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_ENABLE", "true")
    assert model_policy.get_policy_config()["HIBERNATE_ENABLE"] is True
    assert model_policy.hibernate_enabled() is True


def test_hibernate_idle_seconds_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_IDLE_SECONDS", "120")
    assert model_policy.hibernate_idle_seconds() == 120
    monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_IDLE_SECONDS", "not-a-number")
    assert model_policy.hibernate_idle_seconds() == 600


def test_hibernate_stop_media_flips_off(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_STOP_MEDIA", "false")
    assert model_policy.hibernate_stop_media() is False
