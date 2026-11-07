"""Synthetic training data generation service (C10).

DeepSeek-web generates instruction/response training pairs from a topic.
Variables: topic, difficulty, count.

Output format: JSON array of {instruction, response} objects, suitable for
feeding into the existing tiny training pipeline (run_tiny_train).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from open_notebook.exceptions import OpenNotebookError
from open_notebook.services.deepseek_session_service import (
    deepseek_session_service,
)


@dataclass
class SyntheticDataResult:
    """Outcome of synthetic training data generation."""

    ok: bool
    pairs: list[dict] = None  # type: ignore[assignment]
    count: int = 0
    error: Optional[str] = None

    def __post_init__(self):
        if self.pairs is None:
            self.pairs = []


class SyntheticDataGenerator:
    """Generate synthetic instruction/response pairs via DeepSeek-web."""

    _PROMPT_TEMPLATE = """You are a synthetic training data generator. Create instruction/response
training pairs for fine-tuning a small language model.

Topic: {topic}
Difficulty: {difficulty} (1=beginner, 5=expert)
Count: {count}

Output ONLY a JSON array of objects with "instruction" and "response" fields,
nothing else. No markdown fences, no introduction, no conclusion.

Each instruction should be a natural language task or question related to the
topic. Each response should be a helpful, accurate answer appropriate for the
specified difficulty level.

Pairs:"""

    async def generate(
        self,
        topic: str,
        *,
        notebook_id: str,
        difficulty: int = 3,
        count: int = 20,
        classification: str = "tiny_training",
    ) -> SyntheticDataResult:
        """Generate synthetic training pairs via DeepSeek-web.

        Uses the notebook's DeepSeek session series so the model remembers
        context across multiple generation calls.
        """
        if not topic or not topic.strip():
            return SyntheticDataResult(ok=False, error="no topic provided")

        difficulty = max(1, min(5, int(difficulty)))
        count = max(1, min(100, int(count)))  # cap at 100

        prompt = self._PROMPT_TEMPLATE.format(
            topic=topic,
            difficulty=difficulty,
            count=count,
        )

        try:
            result = await deepseek_session_service.send_message(
                notebook_id=notebook_id,
                classification=classification,
                message=prompt,
            )
        except OpenNotebookError as exc:
            logger.warning(f"synthetic data DeepSeek error: {exc}")
            return SyntheticDataResult(ok=False, error=str(exc))

        response_text = result.response
        if not response_text:
            return SyntheticDataResult(
                ok=False, error="empty response from DeepSeek-web"
            )

        pairs = self._parse_pairs(response_text)
        if not pairs:
            return SyntheticDataResult(
                ok=False, error="could not parse training pairs from response"
            )

        return SyntheticDataResult(ok=True, pairs=pairs, count=len(pairs))

    def _parse_pairs(self, response_text: str) -> list[dict]:
        """Parse a JSON array of {instruction, response} objects."""
        text = response_text.strip()

        # Strip markdown fences.
        if text.startswith("```"):
            lines = text.split("\n")
            start = 1
            while start < len(lines) and not lines[start].strip().startswith("["):
                start += 1
            end = start
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            text = "\n".join(lines[start:end]).strip()

        first = text.find("[")
        last = text.rfind("]")
        if first == -1 or last == -1 or last < first:
            return []

        json_text = text[first : last + 1]
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []

        pairs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            instruction = item.get("instruction", "").strip()
            response = item.get("response", "").strip()
            if instruction and response:
                pairs.append({"instruction": instruction, "response": response})

        return pairs


# Module-level singleton.
synthetic_data_generator = SyntheticDataGenerator()
