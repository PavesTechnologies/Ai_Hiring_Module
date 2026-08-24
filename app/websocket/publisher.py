import logging
from uuid import UUID

from app.models.async_tasks import DocumentType
from app.webcore.redis import (
    campaign_board_channel,
    create_pubsub_client,
    jd_channel,
    resume_processing_channel,
)
from app.websocket.events import WebSocketEvent, WebSocketEventType

logger = logging.getLogger(__name__)


def publish_event(
    channel: str,
    event: WebSocketEvent,
) -> bool:
    """
    Publish a WebSocket event through Redis Pub/Sub.

    Returns True when Redis accepts the message, otherwise False.
    """

    redis_client = create_pubsub_client()

    try:
        payload = event.model_dump_json()

        redis_client.publish(
            channel,
            payload,
        )

        logger.debug(
            "WebSocket event published. channel=%s event=%s",
            channel,
            event.event.value,
        )

        return True

    except Exception:
        logger.exception(
            "Failed to publish WebSocket event. channel=%s",
            channel,
        )
        return False

    finally:
        redis_client.close()


# ----------------------------------------------------------------------
# Processing events (JD + Resume pipelines, via StageExecutionService)
# ----------------------------------------------------------------------

def _processing_channel(
    document_type: DocumentType | None,
    task_id: str,
    created_by: str | None,
) -> str | None:
    """
    Resolves which WebSocket channel a processing event belongs on.

    RESUME tasks are watched per-task (GET /resumes/processing-status/{task_id}
    and its WS counterpart), so the Celery task_id alone is enough.

    JD tasks are watched per-user (GET /job-descriptions/my-uploads and its
    WS counterpart, both scoped to created_by=the authenticated user), so a
    JD event needs the task's created_by to route correctly - if it isn't
    available (e.g. an older call site that never threaded it through),
    the event is dropped rather than sent to a wrong/empty channel.
    """
    if document_type == DocumentType.RESUME:
        return resume_processing_channel(task_id)
    if document_type == DocumentType.JD:
        return jd_channel(created_by) if created_by else None
    return None


def publish_stage_completed(
    execution,
    created_by: str | None = None,
) -> None:
    """
    Publishes `stage.completed` for one DocumentProcessingStageExecution
    row. Must only be called once that row's status/error/duration have
    already been committed (see StageExecutionService.complete_stage).
    """
    channel = _processing_channel(execution.document_type, execution.task_id, created_by)
    if channel is None:
        return

    event = WebSocketEvent(
        event=WebSocketEventType.STAGE_COMPLETED,
        data={
            "task_id": execution.task_id,
            "document_type": execution.document_type.value,
            "stage": execution.stage.value,
            "status": execution.status.value,
            "error_message": execution.error_message,
            "duration_ms": execution.duration_ms,
        },
    )
    publish_event(channel, event)


def publish_task_linked(
    task_id: str,
    document_id: UUID,
    document_type: DocumentType | None,
    created_by: str | None = None,
) -> None:
    """
    Publishes `task.linked` once a processing task's document_id has been
    committed (see StageExecutionService.link_document_id).
    """
    channel = _processing_channel(document_type, task_id, created_by)
    if channel is None:
        return

    event = WebSocketEvent(
        event=WebSocketEventType.TASK_LINKED,
        data={
            "task_id": task_id,
            "document_type": document_type.value if document_type else None,
            "document_id": str(document_id),
        },
    )
    publish_event(channel, event)


# ----------------------------------------------------------------------
# Campaign board events (CampaignCandidateService + scoring/eval tasks)
# ----------------------------------------------------------------------

def _publish_board_event(event_type: WebSocketEventType, campaign_id, campaign_candidate) -> None:
    """
    campaign_candidate may be either the CampaignCandidate ORM row or a
    CampaignCandidateResponse schema instance - both expose the same
    id/candidate_id/pipeline_stage attributes the board event needs.
    """
    channel = campaign_board_channel(str(campaign_id))
    event = WebSocketEvent(
        event=event_type,
        data={
            "campaign_id": str(campaign_id),
            "campaign_candidate_id": str(campaign_candidate.id),
            "candidate_id": str(campaign_candidate.candidate_id),
            "pipeline_stage": campaign_candidate.pipeline_stage.value,
        },
    )
    publish_event(channel, event)


def publish_board_candidate_added(campaign_id, campaign_candidate) -> None:
    _publish_board_event(WebSocketEventType.CANDIDATE_ADDED, campaign_id, campaign_candidate)


def publish_board_stage_changed(campaign_id, campaign_candidate) -> None:
    _publish_board_event(WebSocketEventType.STAGE_CHANGED, campaign_id, campaign_candidate)


def publish_board_candidate_removed(campaign_id, campaign_candidate_id) -> None:
    channel = campaign_board_channel(str(campaign_id))
    event = WebSocketEvent(
        event=WebSocketEventType.CANDIDATE_REMOVED,
        data={
            "campaign_id": str(campaign_id),
            "campaign_candidate_id": str(campaign_candidate_id),
        },
    )
    publish_event(channel, event)


def publish_board_candidate_updated(campaign_id, campaign_candidate_id) -> None:
    """
    Used by the scoring/AI-evaluation Celery tasks, which update a
    campaign_candidate's scores directly via the repository rather than
    through CampaignCandidateService - the board's displayed ranking
    changes here even though no CampaignCandidateService method runs.
    """
    channel = campaign_board_channel(str(campaign_id))
    event = WebSocketEvent(
        event=WebSocketEventType.CANDIDATE_UPDATED,
        data={
            "campaign_id": str(campaign_id),
            "campaign_candidate_id": str(campaign_candidate_id),
        },
    )
    publish_event(channel, event)