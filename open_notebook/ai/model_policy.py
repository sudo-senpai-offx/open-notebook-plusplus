# open_notebook/ai/model_policy.py
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_HIBERNATE_IDLE_SECONDS_DEFAULT = 600


def _parse_hibernate_idle_seconds() -> int:
    try:
        return int(os.environ.get("OPEN_NOTEBOOK_HIBERNATE_IDLE_SECONDS", "600"))
    except (TypeError, ValueError):
        return _HIBERNATE_IDLE_SECONDS_DEFAULT


_RESOURCE_WARN_RSS_PCT_DEFAULT = 75


def _parse_resource_warn_rss_pct() -> int:
    try:
        value = int(os.environ.get("OPEN_NOTEBOOK_RESOURCE_WARN_RSS_PCT", "75"))
    except (TypeError, ValueError):
        return _RESOURCE_WARN_RSS_PCT_DEFAULT
    return max(1, min(100, value))


def get_policy_config() -> Dict[str, Any]:
    return {
        "AUTO_MODEL_POLICY": os.environ.get("AUTO_MODEL_POLICY", "offline-first"),
        "CLOUD_ALLOWED": os.environ.get("CLOUD_ALLOWED", "false").lower()
        in ("1", "true", "yes", "on"),
        "PREFER_LOCAL": os.environ.get("PREFER_LOCAL", "true").lower()
        in ("1", "true", "yes", "on"),
        "DEGRADE_GRACEFULLY": os.environ.get("DEGRADE_GRACEFULLY", "true").lower()
        in ("1", "true", "yes", "on"),
        "TIME_BUDGETS_ENFORCED": os.environ.get("TIME_BUDGETS_ENFORCED", "true").lower()
        in ("1", "true", "yes", "on"),
        "MINI_ONLY": os.environ.get(
            "OPEN_NOTEBOOK_MINI_ONLY",
            os.environ.get("MINI_ONLY", "true"),
        ).lower()
        in ("1", "true", "yes", "on"),
        # Training capabilities are opt-in and tier-gated (T4). They default
        # to false so a fresh checkout can never admit heavy training; the
        # tier gate (capability_tier.gate_training) still applies when on.
        "ALLOW_TINY_TRAIN": os.environ.get(
            "OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", "false"
        ).lower()
        in ("1", "true", "yes", "on"),
        "ALLOW_LORA": os.environ.get("OPEN_NOTEBOOK_ALLOW_LORA", "false").lower()
        in ("1", "true", "yes", "on"),
        # Hibernate policy (T3): inert config until a governor reads it.
        # Disabled by default so no existing deployment changes behavior.
        "HIBERNATE_ENABLE": os.environ.get(
            "OPEN_NOTEBOOK_HIBERNATE_ENABLE", "false"
        ).lower()
        in ("1", "true", "yes", "on"),
        "HIBERNATE_IDLE_SECONDS": _parse_hibernate_idle_seconds(),
        "HIBERNATE_STOP_MEDIA": os.environ.get(
            "OPEN_NOTEBOOK_HIBERNATE_STOP_MEDIA", "true"
        ).lower()
        in ("1", "true", "yes", "on"),
        # Resource budgets (T7): percent of detected total system resources.
        # Warn-only; hard caps are explicitly out until governor triggers
        # are proven. Inert until wired.
        "RESOURCE_WARN_RSS_PCT": _parse_resource_warn_rss_pct(),
        "RESOURCE_WARN_ONLY": os.environ.get(
            "OPEN_NOTEBOOK_RESOURCE_WARN_ONLY", "true"
        ).lower()
        in ("1", "true", "yes", "on"),
        "USE_ROUTER": os.environ.get("USE_ROUTER", "false").lower()
        in ("1", "true", "yes", "on"),
        "USE_MIXTURE": os.environ.get("USE_MIXTURE", "false").lower()
        in ("1", "true", "yes", "on"),
        "USE_RERANKER_BEFORE_TRUNCATE": os.environ.get(
            "USE_RERANKER_BEFORE_TRUNCATE", "false"
        ).lower()
        in ("1", "true", "yes", "on"),
    }


def allow_cloud() -> bool:
    return get_policy_config()["CLOUD_ALLOWED"]


def use_router() -> bool:
    return get_policy_config()["USE_ROUTER"]


def use_mixture() -> bool:
    return get_policy_config()["USE_MIXTURE"]


def use_reranker() -> bool:
    return get_policy_config()["USE_RERANKER_BEFORE_TRUNCATE"]


def allow_tiny_train() -> bool:
    return get_policy_config()["ALLOW_TINY_TRAIN"]


def allow_lora() -> bool:
    return get_policy_config()["ALLOW_LORA"]


def hibernate_enabled() -> bool:
    return get_policy_config()["HIBERNATE_ENABLE"]


def hibernate_idle_seconds() -> int:
    return get_policy_config()["HIBERNATE_IDLE_SECONDS"]


def hibernate_stop_media() -> bool:
    return get_policy_config()["HIBERNATE_STOP_MEDIA"]


def resource_warn_rss_pct() -> int:
    return get_policy_config()["RESOURCE_WARN_RSS_PCT"]


def resource_warn_only() -> bool:
    return get_policy_config()["RESOURCE_WARN_ONLY"]
