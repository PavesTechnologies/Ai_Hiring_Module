from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import CompositeScoreTriggerSource

TASKS_MODULE = "app.tasks.composite_scoring_tasks"


def _breakdown():
    return {
        "deterministic_score": 80.0,
        "semantic_score": 0.7,
        "normalized_semantic_score": 70.0,
        "effective_ai_score": None,
        "weight_deterministic": 30.0,
        "weight_semantic": 40.0,
        "weight_ai": 30.0,
        "composite_score": 52.0,
        "formula_version": "v1",
        "trigger_source": "AI_EVALUATION",
        "computed_at": "2026-08-03T10:00:00+00:00",
    }


class _Harness:
    """Mirrors the _Harness pattern in test_semantic_scoring_tasks.py."""

    def __init__(self):
        self.campaign_candidate_repo = MagicMock()
        self.campaign_repo = MagicMock()
        self.history_repo = MagicMock()
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        self.task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
        self.audit_service_instance = MagicMock()
        self.scoring_service_instance = MagicMock()
        self.scoring_service_instance.calculate_and_store_composite_score.return_value = _breakdown()
        self.dead_letter_queue_repo = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.CandidateCompositeScoreHistoryRepository", return_value=self.history_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.CompositeScoringService", return_value=self.scoring_service_instance),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _make_campaign_candidate(
    campaign_id=None, deterministic_score=80, semantic_score=None, effective_ai_score=None,
):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id or uuid4(),
        deterministic_score=deterministic_score, semantic_score=semantic_score,
        effective_ai_score=effective_ai_score,
    )


def _make_campaign(status=CampaignStatus.ACTIVE):
    return SimpleNamespace(id=uuid4(), status=status)


def test_skips_gracefully_when_campaign_candidate_no_longer_exists():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.return_value = None

        calculate_composite_score_task(
            campaign_candidate_id=str(uuid4()), trigger_source="AI_EVALUATION",
        )

        h.scoring_service_instance.calculate_and_store_composite_score.assert_not_called()
        created_log = h.task_log_repo.create.call_args[0][0]
        assert created_log.campaign_candidate_id is None
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_skips_when_campaign_not_scoreable():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        campaign = _make_campaign(status=CampaignStatus.CLOSED)
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="AI_EVALUATION")

        h.scoring_service_instance.calculate_and_store_composite_score.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_computes_even_when_only_one_score_component_is_present():
    """
    Fix 4/5: no "skip when nothing to compute" guard exists anymore - a
    missing component is COALESCEd to 0 by CompositeScoringService itself,
    so the task always calls through to it once its own existence/status
    gates pass, never pre-emptively skipping on missing scores.
    """
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, deterministic_score=None)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="AI_EVALUATION")

        h.scoring_service_instance.calculate_and_store_composite_score.assert_called_once_with(
            cc.id, CompositeScoreTriggerSource.AI_EVALUATION,
        )
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_dead_letters_when_campaign_not_found():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        cc = _make_campaign_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = None

        calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="AI_EVALUATION")

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "COMPOSITE_SCORE"
        assert create_kwargs["campaign_candidate_id"] == cc.id
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD


def test_dead_letters_immediately_on_invalid_scoring_weights_no_retry():
    """
    InvalidScoringWeightsError is a ValueError subclass -> PERMANENT ->
    dead-lettered immediately, never retried (Design Decision 4: abort,
    log, and this is the terminal outcome once retries would be pointless).
    """
    from app.services.campaign.composite_scoring_service import InvalidScoringWeightsError
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_composite_score.side_effect = InvalidScoringWeightsError(
            "weights do not sum to 100.00",
        )

        calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="AI_EVALUATION")

        h.dead_letter_queue_repo.create.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD


def test_retries_on_transient_failure():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_composite_score.side_effect = ConnectionError(
            "db unreachable",
        )

        with pytest.raises(ConnectionError):
            calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="AI_EVALUATION")

        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY


def test_audit_log_and_commit_on_success():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_composite_score_task(campaign_candidate_id=str(cc.id), trigger_source="CAMPAIGN_WEIGHT_CHANGE")

        h.scoring_service_instance.calculate_and_store_composite_score.assert_called_once_with(
            cc.id, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE,
        )
        h.campaign_candidate_repo.commit.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_skips_duplicate_run_when_task_log_already_success():
    from app.tasks.composite_scoring_tasks import calculate_composite_score_task

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        calculate_composite_score_task(campaign_candidate_id=str(uuid4()), trigger_source="AI_EVALUATION")

        h.task_log_repo.create.assert_not_called()
        h.scoring_service_instance.calculate_and_store_composite_score.assert_not_called()


# ----------------------------------------------------------------------
# _enqueue_composite_scoring - shared enqueue/idempotency helper reused by
# both valid trigger sites (campaign weight change, future AI evaluation).
# An HR override is explicitly NOT a trigger site - see
# test_campaign_candidate_override_service.py.
# ----------------------------------------------------------------------

def test_enqueue_composite_scoring_skips_when_already_queued():
    from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring

    campaign_candidate_id = uuid4()
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = [
        SimpleNamespace(status=TaskStatus.QUEUED),
    ]

    with patch(f"{TASKS_MODULE}.calculate_composite_score_task") as task_mock:
        _enqueue_composite_scoring(
            campaign_candidate_id, task_log_service, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE,
        )

        task_log_service.create_log.assert_not_called()
        task_mock.apply_async.assert_not_called()


def test_enqueue_composite_scoring_creates_log_and_dispatches():
    from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring, COMPOSITE_SCORE_TASK_TYPE

    campaign_candidate_id = uuid4()
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = []
    created_log = SimpleNamespace(task_id=str(uuid4()))
    task_log_service.create_log.return_value = created_log

    with patch(f"{TASKS_MODULE}.calculate_composite_score_task") as task_mock:
        _enqueue_composite_scoring(
            campaign_candidate_id, task_log_service, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE,
        )

        create_log_kwargs = task_log_service.create_log.call_args.kwargs
        assert create_log_kwargs["task_type"] == COMPOSITE_SCORE_TASK_TYPE
        assert create_log_kwargs["campaign_candidate_id"] == campaign_candidate_id

        task_mock.apply_async.assert_called_once_with(
            kwargs={
                "campaign_candidate_id": str(campaign_candidate_id),
                "trigger_source": "CAMPAIGN_WEIGHT_CHANGE",
            },
            task_id=created_log.task_id,
        )


def test_enqueue_composite_scoring_swallows_apply_async_failure():
    from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring

    campaign_candidate_id = uuid4()
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = []
    task_log_service.create_log.return_value = SimpleNamespace(task_id=str(uuid4()))

    with patch(f"{TASKS_MODULE}.calculate_composite_score_task") as task_mock:
        task_mock.apply_async.side_effect = Exception("broker unreachable")

        # Must not raise.
        _enqueue_composite_scoring(
            campaign_candidate_id, task_log_service, CompositeScoreTriggerSource.AI_EVALUATION,
        )
