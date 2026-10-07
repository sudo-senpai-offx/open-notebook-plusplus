# open_notebook/ai/model_policy.py
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_policy_config() -> Dict[str, Any]:
    return {
        "AUTO_MODEL_POLICY": os.environ.get("AUTO_MODEL_POLICY", "offline-first"),
        "CLOUD_ALLOWED": os.environ.get("CLOUD_ALLOWED", "false").lower() in ("1", "true", "yes", "on"),
        "PREFER_LOCAL": os.environ.get("PREFER_LOCAL", "true").lower() in ("1", "true", "yes", "on"),
        "DEGRADE_GRACEFULLY": os.environ.get("DEGRADE_GRACEFULLY", "true").lower() in ("1", "true", "yes", "on"),
        "TIME_BUDGETS_ENFORCED": os.environ.get("TIME_BUDGETS_ENFORCED", "true").lower() in ("1", "true", "yes", "on"),
        "USE_ROUTER": os.environ.get("USE_ROUTER", "false").lower() in ("1", "true", "yes", "on"),
        "USE_MIXTURE": os.environ.get("USE_MIXTURE", "false").lower() in ("1", "true", "yes", "on"),
        "USE_RERANKER_BEFORE_TRUNCATE": os.environ.get("USE_RERANKER_BEFORE_TRUNCATE", "false").lower() in ("1", "true", "yes", "on"),
    }


def allow_cloud() -> bool:
    return get_policy_config()["CLOUD_ALLOWED"]


def use_router() -> bool:
    return get_policy_config()["USE_ROUTER"]


def use_mixture() -> bool:
    return get_policy_config()["USE_MIXTURE"]


def use_reranker() -> bool:
    return get_policy_config()["USE_RERANKER_BEFORE_TRUNCATE"]
