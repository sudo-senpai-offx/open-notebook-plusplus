# open_notebook/ai/router.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .capabilities import RoutingDecision, TaskType
from .selector import explain_routing, resolve_routing, should_use_mixture

logger = logging.getLogger(__name__)

Policy = str  # "offline-first" | "balanced" | "speed" | "quality"


def _classify_task(
    task_hint: Optional[str], input_shape: Optional[Dict[str, Any]] = None
) -> TaskType:
    if task_hint:
        t = task_hint.lower()
        if "extract" in t:
            return "extraction"
        if "web" in t or "crawl" in t:
            return "web_extraction"
        if "ocr" in t or "image" in t or "scan" in t:
            return "ocr"
        if "chunk" in t:
            if "code" in t:
                return "code_chunking"
            return "chunking"
        if "embed" in t:
            return "embedding"
        if "transform" in t or "insight" in t:
            return "transformation"
        if "rerank" in t:
            return "rag_query"
        if "reason" in t or "think" in t:
            return "reasoning"
        if "tts" in t or "podcast" in t or "audio" in t:
            return "tts"
        if "stt" in t or "transcrib" in t:
            return "stt"
        if "search" in t or "rag" in t or "query" in t:
            return "rag_query"
        if "chat" in t or "ask" in t:
            return "chat"

    if input_shape:
        if input_shape.get("file_type") in ("pdf", "docx", "pptx", "html", "txt", "md"):
            return "extraction"
        if input_shape.get("url"):
            return "web_extraction"
        if input_shape.get("images") or input_shape.get("needs_ocr"):
            return "ocr"
        if input_shape.get("query") and not input_shape.get("document"):
            return "rag_query"
        if input_shape.get("document") and input_shape.get("instruction"):
            return "transformation"
        if input_shape.get("messages"):
            return "chat"
        if input_shape.get("audio"):
            return "stt"
        if input_shape.get("text_to_speech"):
            return "tts"
        if input_shape.get("chunks"):
            return "embedding"

    return "chat"


def route_task(
    task_hint: Optional[str] = None,
    input_shape: Optional[Dict[str, Any]] = None,
    ctx_tokens: int = 4096,
    policy: Policy = "offline-first",
    allow_cloud: Optional[bool] = None,
) -> RoutingDecision:
    task = _classify_task(task_hint, input_shape)
    if allow_cloud is None:
        if policy == "offline-first":
            allow_cloud = False
        elif policy == "balanced":
            allow_cloud = False
        elif policy == "speed":
            allow_cloud = False
        elif policy == "quality":
            allow_cloud = True
        else:
            allow_cloud = False
    rd = resolve_routing(task=task, ctx_tokens=ctx_tokens, allow_cloud=allow_cloud)
    logger.debug(
        "router: task=%s policy=%s allow_cloud=%s primary=%s",
        task,
        policy,
        allow_cloud,
        rd.primary.name if rd.primary else None,
    )
    return rd


def get_policy() -> Dict[str, Any]:
    return {
        "policy": "offline-first",
        "cloud_allowed": False,
        "prefer_local": True,
        "degrade_gracefully": True,
        "time_budgets_enforced": True,
    }


def should_use_mixture_for(
    task_hint: Optional[str] = None, input_shape: Optional[Dict[str, Any]] = None
) -> bool:
    task = _classify_task(task_hint, input_shape)
    return should_use_mixture(task)


def explain(task_hint: Optional[str] = None, ctx_tokens: int = 4096) -> Dict[str, Any]:
    task = _classify_task(task_hint)
    return explain_routing(task, ctx_tokens)
