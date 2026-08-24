from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WebSocketEventType(str, Enum):
    # Processing events
    STAGE_COMPLETED = "stage.completed"
    TASK_LINKED = "task.linked"

    # Campaign board events
    CANDIDATE_ADDED = "board.candidate_added"
    STAGE_CHANGED = "board.stage_changed"
    CANDIDATE_UPDATED = "board.candidate_updated"
    CANDIDATE_REMOVED = "board.candidate_removed"


class WebSocketEvent(BaseModel):
    event: WebSocketEventType

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    data: dict[str, Any]