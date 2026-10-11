"""T7: resource budgets as percent of detected total system resources.

Denominator is total (MemTotal / cpu count), never free: totals are stable,
free fluctuates. Enforcement is warn-only; hard caps are explicitly out
until the governor proves its triggers.
"""

from __future__ import annotations

from open_notebook.ai import model_policy
from open_notebook.utils import resource_budget
from open_notebook.utils.resource_budget import utilization_pct


def test_parse_memtotal_kb_to_mb():
    text = "MemTotal:        4024548 kB\nMemAvailable:    3145728 kB\n"
    assert resource_budget._parse_memtotal_mb(text) == 3930


def test_parse_memtotal_missing_returns_none():
    assert resource_budget._parse_memtotal_mb("MemAvailable: 1 kB\n") is None


def test_parse_vmrss_kb_to_mb():
    text = "Name:\tpython\nVmRSS:\t   123456 kB\n"
    assert resource_budget._parse_vmrss_mb(text) == 120


def test_utilization_pct_of_total():
    assert utilization_pct(used_mb=1500, total_mb=2000) == 75


def test_utilization_none_safe():
    assert utilization_pct(used_mb=None, total_mb=2000) is None
    assert utilization_pct(used_mb=100, total_mb=None) is None
    assert utilization_pct(used_mb=100, total_mb=0) is None


def test_over_total_clamps_to_100():
    assert utilization_pct(used_mb=3000, total_mb=2000) == 100


def test_resource_policy_defaults(monkeypatch):
    for flag in (
        "OPEN_NOTEBOOK_RESOURCE_WARN_RSS_PCT",
        "OPEN_NOTEBOOK_RESOURCE_WARN_ONLY",
    ):
        monkeypatch.delenv(flag, raising=False)
    config = model_policy.get_policy_config()
    assert config["RESOURCE_WARN_RSS_PCT"] == 75
    assert config["RESOURCE_WARN_ONLY"] is True
    assert model_policy.resource_warn_rss_pct() == 75
    assert model_policy.resource_warn_only() is True


def test_resource_warn_pct_clamps(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_RESOURCE_WARN_RSS_PCT", "not-a-number")
    assert model_policy.resource_warn_rss_pct() == 75
    monkeypatch.setenv("OPEN_NOTEBOOK_RESOURCE_WARN_RSS_PCT", "250")
    assert model_policy.resource_warn_rss_pct() == 100
