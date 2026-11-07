"""DeepSeek session series domain model (A1, A3).

Tracks the mapping between an ON++ session (notebook + classification)
and a series of DeepSeek web sessions (~1M token cap each). Each DeepSeek
web session is a separate conversation on chat.deepseek.com; when one
approaches the token cap, a continuation session is spun up and the old
session is auto-deleted (one-way: ON++ delete cascades to DeepSeek, never
the reverse).

Fields:
  notebook_id                  — ON++ notebook this series belongs to
  deepseek_session_id          — DeepSeek web session id (chat.deepseek.com)
  classification               — chat | insight | embedding | tiny_training
                                 | podcast | flashcard | stt | tts | search
                                 | source_chat
  token_count                  — estimated tokens used in this DeepSeek session
  parent_deepseek_session_id   — previous session in the continuation chain
  status                       — active | completed | archived
  created_at / last_used_at    — timestamps
"""

from datetime import datetime
from typing import ClassVar, List, Optional, Type, TypeVar

from loguru import logger
from pydantic import ConfigDict, field_validator

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.base import ObjectModel
from open_notebook.exceptions import (
    DatabaseOperationError,
    InvalidInputError,
)

T = TypeVar("T", bound="DeepSeekSession")


class DeepSeekSession(ObjectModel):
    """One row per DeepSeek web session in an ON++ session series."""

    table_name: ClassVar[str] = "deepseek_sessions"
    nullable_fields: ClassVar[set[str]] = {
        "deepseek_session_series_id",
        "parent_deepseek_session_id",
    }

    notebook_id: Optional[str] = None
    deepseek_session_id: Optional[str] = None
    classification: Optional[str] = None
    token_count: int = 0
    parent_deepseek_session_id: Optional[str] = None
    status: str = "active"
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    @field_validator("classification")
    @classmethod
    def _valid_classification(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        valid = {
            "chat",
            "insight",
            "embedding",
            "tiny_training",
            "podcast",
            "flashcard",
            "stt",
            "tts",
            "search",
            "source_chat",
        }
        if v not in valid:
            raise InvalidInputError(
                f"Invalid classification '{v}'. Must be one of: {', '.join(sorted(valid))}"
            )
        return v

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        valid = {"active", "completed", "archived"}
        if v not in valid:
            raise InvalidInputError(
                f"Invalid status '{v}'. Must be one of: {', '.join(sorted(valid))}"
            )
        return v

    @classmethod
    async def get_by_deepseek_session_id(
        cls: Type[T], deepseek_session_id: str
    ) -> Optional[T]:
        """Find a DeepSeek session by its DeepSeek web session id."""
        try:
            result = await repo_query(
                "SELECT * FROM deepseek_sessions WHERE deepseek_session_id = $id",
                {"id": deepseek_session_id},
            )
            if result:
                return cls(**result[0])
            return None
        except Exception as e:
            logger.error(f"Error fetching deepseek session {deepseek_session_id}: {e}")
            raise DatabaseOperationError(e)

    @classmethod
    async def get_for_notebook(
        cls: Type[T], notebook_id: str, classification: Optional[str] = None
    ) -> List[T]:
        """List DeepSeek sessions for a notebook, optionally filtered by classification."""
        try:
            if classification:
                result = await repo_query(
                    "SELECT * FROM deepseek_sessions WHERE notebook_id = $nb AND classification = $cl ORDER BY created_at DESC",
                    {"nb": ensure_record_id(notebook_id), "cl": classification},
                )
            else:
                result = await repo_query(
                    "SELECT * FROM deepseek_sessions WHERE notebook_id = $nb ORDER BY created_at DESC",
                    {"nb": ensure_record_id(notebook_id)},
                )
            return [cls(**row) for row in result] if result else []
        except Exception as e:
            logger.error(
                f"Error listing deepseek sessions for notebook {notebook_id}: {e}"
            )
            raise DatabaseOperationError(e)

    @classmethod
    async def get_active_for_notebook(
        cls: Type[T], notebook_id: str, classification: str
    ) -> Optional[T]:
        """Get the active (most recently used) DeepSeek session for a notebook/classification."""
        sessions = await cls.get_for_notebook(notebook_id, classification)
        active = [s for s in sessions if s.status == "active"]
        if not active:
            return None
        # Return the most recently used
        active.sort(key=lambda s: s.last_used_at or s.created_at or "", reverse=True)
        return active[0]

    async def record_usage(self, tokens: int) -> None:
        """Add estimated tokens to this session and update last_used_at."""
        self.token_count = int(self.token_count or 0) + int(tokens)
        self.last_used_at = datetime.now()
        await self.save()

    async def mark_completed(self) -> None:
        """Mark this DeepSeek session as completed (continuation took over)."""
        self.status = "completed"
        self.last_used_at = datetime.now()
        await self.save()

    async def archive(self) -> None:
        """Archive this DeepSeek session (one-way delete cascade target)."""
        self.status = "archived"
        self.last_used_at = datetime.now()
        await self.save()

    model_config = ConfigDict(
        validate_assignment=True,
        arbitrary_types_allowed=True,
        extra="allow",
        from_attributes=True,
        defer_build=True,
    )


# Token estimation: DeepSeek web provides no native token counter, so we
# estimate characters / 4 (conservative for CJK/mixed content).
def estimate_tokens(text: str) -> int:
    """Estimate token count from text length (chars / 4, rounded up)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
