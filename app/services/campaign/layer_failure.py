"""
Scorecard layer failures: which of a campaign candidate's scoring layers
stopped mid-pipeline because its task was dead-lettered.

Read-only and best-effort - every caller attaches the result to an existing
response, so a lookup failure is logged and treated as "no failure" rather
than breaking the endpoint it rides on.
"""
import logging

from app.models.pipeline import AIEvaluationStatus
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.schemas.campaign.layer_failure_schema import LayerFailureResponse
from app.services.campaign.dlq_error_summary import summarize_dlq_error

logger = logging.getLogger(__name__)

LAYER_DETERMINISTIC = "deterministic"
LAYER_SEMANTIC = "semantic"
LAYER_AI = "ai"

# Candidate-level tasks, keyed by campaign_candidate_id on the DLQ row.
_CANDIDATE_TASK_LAYER = {
    "DETERMINISTIC_SCORE": LAYER_DETERMINISTIC,
    "SEMANTIC_SCORE": LAYER_SEMANTIC,
    "AI_EVALUATE": LAYER_AI,
}
# Resume-level tasks, keyed by resume_id only (they run before/independent of
# any campaign_candidate): a missing resume embedding is why semantic is empty.
_RESUME_TASK_LAYER = {
    "EMBED_RESUME": LAYER_SEMANTIC,
}

# Must match CampaignService._DLQ_REPLAY_BUILDERS' keys - kept as a literal
# (not imported) because campaign_service imports the Celery task modules;
# test_layer_failure.py asserts the two stay in sync.
REPLAYABLE_TASK_TYPES = frozenset({
    "DETERMINISTIC_SCORE", "RESUME_DOCUMENT_PROCESSING", "EMBED_RESUME", "AI_EVALUATE",
})

# An AI evaluation in one of these has a result to show - an older DLQ entry
# is stale then (e.g. a later manual rescore succeeded).
_AI_RESULT_STATUSES = frozenset({AIEvaluationStatus.COMPLETED, AIEvaluationStatus.MANUAL_REVIEW})


def _layer_has_result(campaign_candidate, layer: str) -> bool:
    if layer == LAYER_DETERMINISTIC:
        return getattr(campaign_candidate, "deterministic_score", None) is not None
    if layer == LAYER_SEMANTIC:
        return bool(getattr(campaign_candidate, "semantic_breakdown", None))
    if layer == LAYER_AI:
        ai_evaluation = getattr(campaign_candidate, "ai_evaluation", None)
        return getattr(ai_evaluation, "ai_evaluation_status", None) in _AI_RESULT_STATUSES
    return False


def get_layer_failures(
    dead_letter_queue_repo: DeadLetterQueueRepository,
    campaign_candidate,
) -> dict[str, LayerFailureResponse]:
    """
    {layer: most recent open failure} for this candidate. Only layers that
    still have no result are included, and only DLQ rows not yet replayed or
    resolved - a replayed row that fails again dead-letters a new row.
    """
    try:
        entries = dead_letter_queue_repo.get_open_for_campaign_candidate(
            campaign_candidate_id=campaign_candidate.id,
            candidate_task_types=list(_CANDIDATE_TASK_LAYER),
            resume_id=getattr(campaign_candidate, "resume_id", None),
            resume_task_types=list(_RESUME_TASK_LAYER),
        )
        failures: dict[str, LayerFailureResponse] = {}
        for entry in entries:  # newest first
            layer = _CANDIDATE_TASK_LAYER.get(entry.task_type) or _RESUME_TASK_LAYER.get(entry.task_type)
            if layer is None or layer in failures or _layer_has_result(campaign_candidate, layer):
                continue
            failures[layer] = LayerFailureResponse(
                dlq_id=entry.id,
                layer=layer,
                task_type=entry.task_type,
                error_message=entry.final_error_message,
                error_summary=summarize_dlq_error(entry.final_error_message),
                retry_count=entry.retry_count,
                moved_to_dlq_at=entry.moved_to_dlq_at,
                last_attempted_at=entry.last_attempted_at,
                can_retry=entry.task_type in REPLAYABLE_TASK_TYPES,
            )
        return failures
    except Exception:
        logger.exception(
            "Layer failure lookup failed for campaign_candidate_id=%s - reporting none.",
            getattr(campaign_candidate, "id", None),
        )
        # A failed SELECT aborts the Postgres transaction - roll back so the
        # rest of the request's reads still work.
        try:
            dead_letter_queue_repo.rollback()
        except Exception:
            pass
        return {}
