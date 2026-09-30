from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import AIEvaluationStatus
from app.services.campaign.layer_failure import (
    LAYER_AI,
    LAYER_DETERMINISTIC,
    LAYER_SEMANTIC,
    REPLAYABLE_TASK_TYPES,
    get_layer_failures,
)

"""
Scorecard per-layer failure info - which scoring layer a campaign candidate
stopped at because its task dead-lettered. Attached additively to the tab
endpoints / parsed-json, so it must never raise and never report a layer
that has a result.
"""


def _candidate(deterministic_score=None, semantic_breakdown=None, ai_status=None):
    return SimpleNamespace(
        id=uuid4(),
        resume_id=uuid4(),
        deterministic_score=deterministic_score,
        semantic_breakdown=semantic_breakdown,
        ai_evaluation=SimpleNamespace(ai_evaluation_status=ai_status) if ai_status else None,
    )


def _entry(task_type, minutes_ago=0, error="boom"):
    moved = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return SimpleNamespace(
        id=uuid4(), task_type=task_type, final_error_message=error, retry_count=1,
        moved_to_dlq_at=moved, last_attempted_at=moved,
    )


def _repo(entries):
    repo = MagicMock()
    repo.get_open_for_campaign_candidate.return_value = entries
    return repo


def test_ai_failure_reported_when_ai_has_no_result():
    cc = _candidate(deterministic_score=80, semantic_breakdown={"semantic_passed": True})
    entry = _entry("AI_EVALUATE", error="429 RESOURCE_EXHAUSTED")

    failures = get_layer_failures(_repo([entry]), cc)

    assert set(failures) == {LAYER_AI}
    failure = failures[LAYER_AI]
    assert failure.dlq_id == entry.id
    assert failure.task_type == "AI_EVALUATE"
    assert failure.error_message == "429 RESOURCE_EXHAUSTED"
    assert failure.can_retry is True


def test_stale_entry_ignored_once_layer_has_result():
    cc = _candidate(deterministic_score=80, ai_status=AIEvaluationStatus.COMPLETED)
    repo = _repo([_entry("AI_EVALUATE"), _entry("DETERMINISTIC_SCORE")])

    assert get_layer_failures(repo, cc) == {}


def test_failed_ai_status_still_reports_failure():
    cc = _candidate(deterministic_score=80, ai_status=AIEvaluationStatus.FAILED)

    assert set(get_layer_failures(_repo([_entry("AI_EVALUATE")]), cc)) == {LAYER_AI}


def test_newest_entry_wins_per_layer():
    cc = _candidate()
    newer, older = _entry("DETERMINISTIC_SCORE", 1), _entry("DETERMINISTIC_SCORE", 30)

    failures = get_layer_failures(_repo([newer, older]), cc)  # repo returns newest first

    assert failures[LAYER_DETERMINISTIC].dlq_id == newer.id


def test_semantic_failures_not_replayable_but_embed_resume_is():
    cc = _candidate(deterministic_score=80)

    semantic = get_layer_failures(_repo([_entry("SEMANTIC_SCORE")]), cc)[LAYER_SEMANTIC]
    embed = get_layer_failures(_repo([_entry("EMBED_RESUME")]), cc)[LAYER_SEMANTIC]

    assert semantic.can_retry is False
    assert embed.can_retry is True


def test_repo_queried_with_candidate_and_resume_scope():
    cc = _candidate()
    repo = _repo([])

    get_layer_failures(repo, cc)

    kwargs = repo.get_open_for_campaign_candidate.call_args.kwargs
    assert kwargs["campaign_candidate_id"] == cc.id
    assert kwargs["resume_id"] == cc.resume_id
    assert set(kwargs["candidate_task_types"]) == {"DETERMINISTIC_SCORE", "SEMANTIC_SCORE", "AI_EVALUATE"}
    assert set(kwargs["resume_task_types"]) == {"EMBED_RESUME"}


def test_lookup_error_reports_none_and_rolls_back():
    repo = MagicMock()
    repo.get_open_for_campaign_candidate.side_effect = RuntimeError("db down")

    assert get_layer_failures(repo, _candidate()) == {}
    repo.rollback.assert_called_once()


def test_replayable_types_match_campaign_replay_registry():
    from app.services.campaign.campaign_service import CampaignService

    assert REPLAYABLE_TASK_TYPES == set(CampaignService._DLQ_REPLAY_BUILDERS)
