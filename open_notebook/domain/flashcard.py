import hashlib
from typing import ClassVar, List, Literal, Optional

from open_notebook.domain.base import ObjectModel

CardModel = Literal["basic", "cloze"]
CardStatus = Literal["draft", "approved", "tombstoned"]


def make_guid(source_id: str, kind: str, index: int) -> str:
    """Content-stable 16-hex GUID so `.apkg` re-exports dedupe additively."""
    digest = hashlib.sha256(f"{source_id}\0{kind}\0{index}".encode("utf-8"))
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
