import hashlib
from typing import ClassVar, List, Literal, Optional

from open_notebook.domain.base import ObjectModel

CardModel = Literal["basic", "cloze"]
CardStatus = Literal["draft", "approved", "tombstoned"]


def content_guid(
    model: str,
    front: Optional[str] = None,
    back: Optional[str] = None,
    cloze_text: Optional[str] = None,
) -> str:
    """GUID from card content: identical cards share an identity (dedupe by
    construction, no counter query, no race)."""
    digest = hashlib.sha256(
        "\0".join([model, front or "", back or "", cloze_text or ""]).encode("utf-8")
    )
    return digest.hexdigest()[:16]


class Flashcard(ObjectModel):
    """One reviewable unit. Lifecycle: draft -> approved -> tombstoned."""

    table_name: ClassVar[str] = "flashcard"
    source_id: Optional[str] = None
    model: CardModel = "basic"
    front: Optional[str] = None
    back: Optional[str] = None
    cloze_text: Optional[str] = None
    tags: List[str] = []
    guid: Optional[str] = None
    status: CardStatus = "draft"
