# open_notebook/ai/agent_registry.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    max_parallel_tasks: int = 1
    prefers_local: bool = True
    allowed_providers: tuple[str, ...] = field(
        default_factory=lambda: ("ollama", "onnx", "vllm")
    )


AGENTS: List[AgentSpec] = [
    AgentSpec(
        name="extractor",
        role="extraction",
        tools=("extract", "ocr", "normalize"),
        max_parallel_tasks=1,
    ),
    AgentSpec(
        name="chunker",
        role="chunking",
        tools=("chunk", "code_chunk"),
        max_parallel_tasks=2,
    ),
    AgentSpec(
        name="transformer",
        role="transformation",
        tools=("summarize", "insight", "rewrite"),
        max_parallel_tasks=1,
    ),
    AgentSpec(
        name="embedder",
        role="embedding",
        tools=("embed", "batch_embed"),
        max_parallel_tasks=1,
    ),
    AgentSpec(
        name="retriever",
        role="rag_query",
        tools=("search", "rerank"),
        max_parallel_tasks=2,
    ),
    AgentSpec(
        name="reasoner",
        role="reasoning",
        tools=("plan", "think", "verify"),
        max_parallel_tasks=1,
    ),
    AgentSpec(
        name="chat_agent", role="chat", tools=("answer", "ground"), max_parallel_tasks=1
    ),
    AgentSpec(
        name="tts_agent", role="tts", tools=("speak", "mix"), max_parallel_tasks=1
    ),
    AgentSpec(
        name="stt_agent", role="stt", tools=("transcribe",), max_parallel_tasks=1
    ),
]


def get_agent(name: str) -> Optional[AgentSpec]:
    for a in AGENTS:
        if a.name == name:
            return a
    return None


def list_agents() -> List[AgentSpec]:
    return list(AGENTS)


def agents_for_role(role: str) -> List[AgentSpec]:
    return [a for a in AGENTS if a.role == role]
