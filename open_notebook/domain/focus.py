from datetime import datetime
from typing import ClassVar, Literal, Optional

from open_notebook.domain.base import ObjectModel

FocusStatus = Literal["running", "done", "cancelled"]


class FocusSession(ObjectModel):
    """A pomodoro focus session lease. Running sessions veto hibernate.

    Rows are append-only history: ending sets status + ended_at, never edits
    anything else.
    """

    table_name: ClassVar[str] = "focus_session"
    notebook_id: Optional[str] = None
    preset: str = "pomodoro"
    planned_seconds: int = 1500
    status: FocusStatus = "running"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
