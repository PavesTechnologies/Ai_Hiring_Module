"""
Review item 21: the 15-minute semantic recovery scan no longer re-queues a permanently failing
candidate forever. It looks only at candidates still being screened on scoreable campaigns, leaves a
real terminal failure for a person, and does re-queue what it exists for: a candidate whose semantic
scoring was skipped for a missing resume embedding that has since landed.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.async_tasks import TaskStatus

MODULE = "app.tasks.semantic_scoring_tasks"


def _enqueue(existing, *, from_recovery_scan=True, decision_reason=None):
    from app.tasks import semantic_scoring_tasks as mod
    existing.task_id = "old"
    existing.task_type = "SEMANTIC_SCORE"
    existing.queued_at = datetime.now(timezone.utc)
    existing.started_at = None
    repo = MagicMock()
    repo.get_by_idempotency_key.return_value = existing
    repo.update.side_effect = lambda log: log
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    candidate = SimpleNamespace(id=uuid4(), resume_id=uuid4(), semantic_score=None, decision_reason=decision_reason)
    with patch.object(mod.calculate_semantic_score_task, "apply_async") as apply_async:
        mod._enqueue_semantic_scoring(candidate, MagicMock(repository=repo), resume_repo,
                                      from_recovery_scan=from_recovery_scan)
    return apply_async.called


@pytest.mark.parametrize("status", [TaskStatus.DEAD, TaskStatus.FAILURE])
def test_the_scan_leaves_a_terminal_failure_for_a_person(status):
    assert _enqueue(SimpleNamespace(status=status, dispatch_failed=False)) is False


def test_other_triggers_may_still_retry_a_terminal_failure():
    """An HR override or the embedding trigger is a deliberate re-trigger, as before."""
    assert _enqueue(SimpleNamespace(status=TaskStatus.DEAD, dispatch_failed=False), from_recovery_scan=False) is True


def test_the_scan_requeues_a_lost_row():
    assert _enqueue(SimpleNamespace(status=TaskStatus.FAILURE, dispatch_failed=True)) is True


def test_a_missing_embedding_skip_is_queued_again_once_the_embedding_exists():
    from app.tasks.semantic_scoring_tasks import MISSING_RESUME_EMBEDDING_REASON
    skipped = SimpleNamespace(status=TaskStatus.SUCCESS, dispatch_failed=False)
    assert _enqueue(skipped, decision_reason=MISSING_RESUME_EMBEDDING_REASON) is True
    assert _enqueue(SimpleNamespace(status=TaskStatus.SUCCESS, dispatch_failed=False),
                    decision_reason="Semantic score below threshold") is False


def test_the_scan_asks_only_for_screening_candidates_on_scoreable_campaigns():
    from app.tasks import semantic_scoring_tasks as mod
    candidates = MagicMock()
    candidates.get_pending_semantic_score_with_ready_embedding.return_value = []
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.CampaignCandidateRepository", return_value=candidates), \
            patch(f"{MODULE}.CeleryTaskLogRepository", return_value=MagicMock()):
        mod.recover_pending_semantic_scores()
    statuses = candidates.get_pending_semantic_score_with_ready_embedding.call_args.args[0]
    assert {s.value for s in statuses} == {"ACTIVE", "PAUSED"}


def test_the_scan_query_filters_stage_and_campaign():
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    from app.models.campaigns import CampaignStatus
    db = MagicMock()
    captured = {}
    query = MagicMock()
    db.query.return_value = query
    query.join.return_value = query
    query.distinct.return_value = query
    query.all.return_value = []

    def record(*conditions):
        captured.setdefault("filters", []).extend(conditions)
        return query
    query.filter.side_effect = record
    CampaignCandidateRepository(db).get_pending_semantic_score_with_ready_embedding({CampaignStatus.ACTIVE})
    sql = " ".join(str(c.compile(dialect=postgresql.dialect())) for c in captured["filters"]).lower()
    assert "campaign_candidates.pipeline_stage in" in sql and "hiring_campaigns.status in" in sql
