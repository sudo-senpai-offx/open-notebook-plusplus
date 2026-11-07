"""Flashcard generation service (C12).

DeepSeek-web generates Q&A flashcard pairs from source text; local FSRS
handles scheduling. Variables: source_text, language, card_type.

Cards land as drafts for the approval gate (same as manually authored
cards in api/routers/flashcards.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from open_notebook.domain.flashcard import CardModel, Flashcard, content_guid
from open_notebook.exceptions import OpenNotebookError
from open_notebook.services.deepseek_session_service import (
    deepseek_session_service,
)


@dataclass
class FlashcardGenerationResult:
    """Outcome of flashcard generation."""

    ok: bool
    cards: list[dict] = None  # type: ignore[assignment]
    count: int = 0
    error: Optional[str] = None

    def __post_init__(self):
        if self.cards is None:
            self.cards = []


class FlashcardGenerator:
    """Generate flashcards from source text via DeepSeek-web."""

    _PROMPT_TEMPLATE = """You are a flashcard generator. Convert the source text into Q&A flashcards.

Source text:
{source_text}

Language: {language}
Card type: {card_type}

Generate {count} flashcards. For each card, output a JSON object with:
- "front": the question or prompt
- "back": the answer
- For cloze cards: "cloze_text" with the full text and "{{{c1::answer}}}" markers

Output ONLY a JSON array of card objects, nothing else. No markdown fences, no explanation.

Cards:"""

    async def generate(
        self,
        source_text: str,
        *,
        notebook_id: str,
        source_id: Optional[str] = None,
        language: str = "en",
        card_type: CardModel = "basic",
        count: int = 10,
        classification: str = "flashcard",
    ) -> FlashcardGenerationResult:
        """Generate flashcards from source text via DeepSeek-web.

        Cards are saved as drafts (approval gate). Returns generated cards.
        """
        if not source_text or not source_text.strip():
            return FlashcardGenerationResult(
                ok=False, error="no source text to generate from"
            )

        count = max(1, min(count, 50))  # cap at 50

        prompt = self._PROMPT_TEMPLATE.format(
            source_text=source_text[:12000],  # cap prompt length
            language=language,
            card_type=card_type,
            count=count,
        )

        try:
            result = await deepseek_session_service.send_message(
                notebook_id=notebook_id,
                classification=classification,
                message=prompt,
            )
        except OpenNotebookError as exc:
            logger.warning(f"flashcard generation DeepSeek error: {exc}")
            return FlashcardGenerationResult(ok=False, error=str(exc))

        response_text = result.response
        if not response_text:
            return FlashcardGenerationResult(
                ok=False, error="empty response from DeepSeek-web"
            )

        # Parse JSON array from response (strip any surrounding text).
        cards = self._parse_cards(response_text, card_type)
        if not cards:
            return FlashcardGenerationResult(
                ok=False, error="could not parse flashcards from response"
            )

        # Save cards as drafts.
        saved = []
        for card_data in cards:
            try:
                card = Flashcard(
                    model=card_type,
                    front=card_data.get("front"),
                    back=card_data.get("back"),
                    cloze_text=card_data.get("cloze_text"),
                    tags=["generated", language],
                    source_id=source_id,
                    status="draft",
                )
                # Dedupe by content GUID.
                card.guid = content_guid(
                    card_type,
                    card_data.get("front"),
                    card_data.get("back"),
                    card_data.get("cloze_text"),
                )
                await card.save()
                saved.append(
                    {
                        "id": str(card.id),
                        "guid": card.guid,
                        "model": card.model,
                        "front": card.front,
                        "back": card.back,
                        "cloze_text": card.cloze_text,
                        "tags": list(card.tags or []),
                        "source_id": card.source_id,
                        "status": card.status,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one bad card doesn't fail all
                logger.warning(f"failed to save flashcard: {exc}")
                continue

        return FlashcardGenerationResult(
            ok=True,
            cards=saved,
            count=len(saved),
        )

    def _parse_cards(self, response_text: str, card_type: CardModel) -> list[dict]:
        """Parse a JSON array of card objects from the model response.

        Handles: pure JSON, JSON in markdown fences, JSON with surrounding text.
        """
        text = response_text.strip()

        # Strip markdown fences.
        if text.startswith("```"):
            lines = text.split("\n")
            # Find the JSON between fences.
            start = 1
            while start < len(lines) and not lines[start].strip().startswith("["):
                start += 1
            end = start
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            text = "\n".join(lines[start:end]).strip()

        # Find the first '[' and last ']' to extract the array.
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

        cards = []
        for item in data:
            if not isinstance(item, dict):
                continue
            front = item.get("front")
            back = item.get("back")
            cloze_text = item.get("cloze_text")
            if card_type == "basic" and front and back:
                cards.append({"front": front, "back": back})
            elif card_type == "cloze" and cloze_text:
                cards.append({"cloze_text": cloze_text})

        return cards


# Module-level singleton.
flashcard_generator = FlashcardGenerator()
