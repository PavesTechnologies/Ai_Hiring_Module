from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class LayerFailureResponse(BaseModel):
    """
    A scoring layer's task that dead-lettered for one campaign candidate,
    and whose result is therefore missing. Rides on that layer's own
    scorecard-tab response (`failure`) and on the parsed-json response
    (`failed_layers`, all layers), so the scorecard can show why a tab is
    empty and offer a retry without a separate DLQ lookup. `dlq_id` is what
    POST /campaigns/{campaign_id}/dead-letter-queue/replay takes; that
    replay re-runs only this task, not the layers before it.
    """

    dlq_id: UUID
    layer: str                        # "deterministic" | "semantic" | "ai"
    task_type: str
    error_message: str                # raw provider/exception text - same as the DLQ list's final_error_message
    error_summary: str = ""            # short plain phrase for the UI
    retry_count: int
    moved_to_dlq_at: datetime
    last_attempted_at: datetime | None = None
    can_retry: bool                   # task_type is re-enqueueable by the campaign DLQ replay endpoint
