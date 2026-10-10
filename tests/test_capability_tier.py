"""T2: capability-tier predicate pins.

The tier gate decides whether a training feature may run on this machine
right now. LoRA is denied below the low-end gate (unknown memory fails safe);
tiny/tabular stay open on the 2GB tier. Reasons must stay actionable.
"""

from __future__ import annotations

import pytest

from open_notebook.utils import capability_tier
from open_notebook.utils.capability_tier import tier_allows


def test_parse_memavailable_kb_to_mb():
    text = "MemTotal:        4024548 kB\nMemFree:          123456 kB\nMemAvailable:    3145728 kB\n"
    assert capability_tier._parse_memavailable_mb(text) == 3072


def test_parse_memavailable_missing_returns_none():
    assert capability_tier._parse_memavailable_mb("MemTotal: 4024548 kB\n") is None


def test_lora_allowed_above_gate(monkeypatch):
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 4096)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)
    allowed, reason = tier_allows("lora")
    assert allowed is True
    assert reason


def test_lora_denied_below_memory_gate(monkeypatch):
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 1500)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)
    allowed, reason = tier_allows("lora")
    assert allowed is False
    assert "1500" in reason


def test_lora_denied_single_thread(monkeypatch):
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 4096)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 1)
    allowed, _ = tier_allows("lora")
    assert allowed is False


def test_lora_denied_when_memory_unknown(monkeypatch):
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: None)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)
    allowed, reason = tier_allows("lora")
    assert allowed is False
    assert reason


@pytest.mark.parametrize("feature", ["tiny_train", "tabular"])
def test_light_features_open_on_2gb_tier(monkeypatch, feature):
    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 1500)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 2)
    allowed, _ = tier_allows(feature)
    assert allowed is True


def test_unknown_feature_raises():
    with pytest.raises(ValueError, match="unknown capability"):
        tier_allows("pretrain_70b")
