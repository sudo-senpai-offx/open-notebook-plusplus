"""T4: training flags stay OFF unless explicitly opted in, and the gate
combines flag + tier verdict. A denied request names the fallback instead
of silently substituting it (no auto-fallback routing: T5/T6 unbuilt).
"""

from __future__ import annotations

import pytest

from open_notebook.ai import model_policy
from open_notebook.utils import capability_tier
from open_notebook.utils.capability_tier import gate_training

_POLICY_TRAINING_FLAGS = (
    "OPEN_NOTEBOOK_ALLOW_TINY_TRAIN",
    "OPEN_NOTEBOOK_ALLOW_LORA",
)


@pytest.fixture(autouse=True)
def _clean_training_env(monkeypatch):
    for flag in _POLICY_TRAINING_FLAGS:
        monkeypatch.delenv(flag, raising=False)


def test_training_flags_default_false():
    config = model_policy.get_policy_config()
    assert config["ALLOW_TINY_TRAIN"] is False
    assert config["ALLOW_LORA"] is False
    assert model_policy.allow_tiny_train() is False
    assert model_policy.allow_lora() is False


@pytest.mark.parametrize(
    ("env_flag", "key"),
    [
        ("OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", "ALLOW_TINY_TRAIN"),
        ("OPEN_NOTEBOOK_ALLOW_LORA", "ALLOW_LORA"),
    ],
)
def test_training_flags_flip_on_when_set(monkeypatch, env_flag, key):
    monkeypatch.setenv(env_flag, "true")
    assert model_policy.get_policy_config()[key] is True


def test_gate_denies_lora_when_flag_off_even_on_big_box(monkeypatch):
    # Flag off wins over a passing tier: no silent admission.
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 8192)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 8)
    allowed, reason = gate_training("lora")
    assert allowed is False
    assert "OPEN_NOTEBOOK_ALLOW_LORA" in reason


def test_gate_defers_to_tier_when_flag_on(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_ALLOW_LORA", "true")
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 1500)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 2)
    allowed, reason = gate_training("lora")
    assert allowed is False
    assert "tiny/tabular" in reason


def test_gate_opens_lora_when_flag_on_and_tier_passes(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_ALLOW_LORA", "true")
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 4096)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)
    allowed, _ = gate_training("lora")
    assert allowed is True


def test_gate_unknown_feature_raises():
    with pytest.raises(ValueError, match="unknown capability"):
        gate_training("pretrain_70b")
