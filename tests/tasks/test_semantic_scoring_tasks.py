from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage

TASKS_MODULE = "app.tasks.semantic_scoring_tasks"


def _breakdown(passed=True, similarity=0.8, threshold=0.65):
    return {
        "semantic_score": similarity,
        "overall_similarity": similarity,
        "semantic_passed": passed,
        "semantic_threshold": threshold,
        "matching_skills": ["Python"],
        "missing_skills": [] if passed else ["Kubernetes"],
        "matched_keywords": ["python"],
        "semantic_explanation": "Resume-to-job semantic similarity is 80.0%.",
        "resume_embedding_model_version_id": str(uuid4()),
        "jd_embedding_model_version_id": str(uuid4()),
        "computed_at": "2026-07-29T10:00:00+00:00",
    }


class _Harness:
    """Mirrors the _Harness pattern in test_deterministic_scoring_tasks.py / test_embedding_tasks.py."""

    def __init__(self):
        self.campaign_candidate_repo = MagicMock()
        self.campaign_repo = MagicMock()
        self.resume_repo = MagicMock()
        self.jd_repo = MagicMock()
        self.config_repo = MagicMock()
        self.config_repo.get_configs_by_keys.return_value = {}
        self.candidate_rejection_repo = MagicMock()
        self.allowed_transition_repo = MagicMock()
        self.allowed_transition_repo.is_transition_allowed.return_value = True
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        # Default: no queued AI_EVALUATE logs, so _cancel_downstream_ai_evaluation
        # (run for real - it's fully generic and safe against mocks) is a no-op.
        self.task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
        self.audit_service_instance = MagicMock()
        self.scoring_service_instance = MagicMock()
        self.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown()
        self.dead_letter_queue_repo = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.CandidateRejectionRepository", return_value=self.candidate_rejection_repo),
            patch(f"{TASKS_MODULE}.AllowedTransitionRepository", return_value=self.allowed_transition_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.SemanticScoringService", return_value=self.scoring_service_instance),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _make_campaign_candidate(
    campaign_id=None, resume_id=None, pipeline_stage=PipelineStage.SCREENING,
    deterministic_passed=True, candidate_id=None,
):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id or uuid4(), resume_id=resume_id or uuid4(),
        candidate_id=candidate_id or uuid4(), pipeline_stage=pipeline_stage,
        deterministic_passed=deterministic_passed,
    )


def _make_campaign(status=CampaignStatus.ACTIVE, jd_id=None):
    return SimpleNamespace(id=uuid4(), status=status, jd_id=jd_id or uuid4())


def test_skips_gracefully_when_campaign_candidate_no_longer_exists():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.return_value = None

        calculate_semantic_score_task(campaign_candidate_id=str(uuid4()))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        created_log = h.task_log_repo.create.call_args[0][0]
        assert created_log.campaign_candidate_id is None
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_skips_when_campaign_not_scoreable():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign(status=CampaignStatus.CLOSED)
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_skips_when_deterministic_not_passed():
    """
    Semantic scoring only applies once a candidate has cleared the
    deterministic layer - a candidate that failed (or was never scored)
    must be skipped gracefully, never scored.
    """
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, deterministic_passed=None)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_dead_letters_when_campaign_not_found():
    """A missing campaign is a ValueError -> PERMANENT -> dead-lettered immediately, no retry."""
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        cc = _make_campaign_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = None

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "SEMANTIC_SCORE"
        assert create_kwargs["campaign_candidate_id"] == cc.id
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD


def test_retries_on_transient_failure():
    """
    A missing resume/JD embedding (or any non-ValueError/KeyError/TypeError
    exception) classifies as retryable - must retry, never dead-letter, on
    the first attempt. Celery's Task.retry(exc=ex) re-raises the original
    exception when called directly (no real worker context), same as the
    equivalent test in test_embedding_tasks.py.
    """
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.side_effect = ConnectionError(
            "db unreachable",
        )

        with pytest.raises(ConnectionError):
            calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY


def test_missing_embedding_error_is_retried():
    """MissingResumeEmbeddingError/MissingJDEmbeddingError are plain Exception subclasses -> UNKNOWN -> retryable."""
    from app.services.campaign.semantic_scoring_service import MissingResumeEmbeddingError
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.side_effect = (
            MissingResumeEmbeddingError("no embedding yet")
        )

        with pytest.raises(MissingResumeEmbeddingError):
            calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY


def test_no_rejection_when_semantic_score_passes():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=True)

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_not_called()
        assert cc.pipeline_stage == PipelineStage.SCREENING
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_creates_rejection_and_transitions_stage_when_semantic_score_below_threshold():
    from app.models.pipeline import RejectionLayer
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        breakdown = _breakdown(passed=False, similarity=0.3)
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = breakdown

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_layer == RejectionLayer.SEMANTIC
        assert rejection.rejection_detail == breakdown

        h.allowed_transition_repo.is_transition_allowed.assert_called_once_with(
            PipelineStage.SCREENING, PipelineStage.REJECTED,
        )
        assert cc.pipeline_stage == PipelineStage.REJECTED
        h.campaign_candidate_repo.create_stage_history.assert_called_once()


def test_cancels_downstream_ai_evaluation_on_semantic_rejection():
    from app.models.async_tasks import CeleryTaskLog
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=False)

        queued_ai_eval_log = CeleryTaskLog(
            task_id=str(uuid4()), task_type="AI_EVALUATE", status=TaskStatus.QUEUED,
        )
        h.task_log_repo.get_by_campaign_candidate_and_task_type.return_value = [queued_ai_eval_log]

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        assert queued_ai_eval_log.status == TaskStatus.DEAD


def test_audit_log_records_semantic_score_computed():
    from app.enums.constants import ActionType, EntityType
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        breakdown = _breakdown(passed=True)
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = breakdown

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.audit_service_instance.log.assert_called_once()
        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["action_type"] == ActionType.SEMANTIC_SCORE_COMPUTED
        assert audit_kwargs["entity_type"] == EntityType.CAMPAIGN_CANDIDATE
        assert audit_kwargs["entity_id"] == cc.id
        assert audit_kwargs["details"]["semantic_score"] == breakdown["semantic_score"]
        h.campaign_candidate_repo.commit.assert_called_once()


def test_skips_duplicate_run_when_task_log_already_success():
    """
    Same broker-redelivery guard as calculate_deterministic_score_task -
    campaign_candidate existence is still checked first (FK-safety, same
    ordering reasoning as that task), but scoring itself never runs twice
    for a task_id that already completed.
    """
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        calculate_semantic_score_task(campaign_candidate_id=str(uuid4()))

        h.task_log_repo.create.assert_not_called()
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()


# ----------------------------------------------------------------------
# _enqueue_semantic_scoring - the shared enqueue/idempotency helper reused
# by both calculate_deterministic_score_task's auto-trigger (after a
# successful pass) and CampaignCandidateService._queue_post_override_evaluation.
# ----------------------------------------------------------------------

def _make_campaign_candidate_for_enqueue(resume_id=None):
    return SimpleNamespace(id=uuid4(), resume_id=resume_id or uuid4())


def test_enqueue_semantic_scoring_noop_when_resume_has_no_embedding():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = None
    task_log_service = MagicMock()

    _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

    task_log_service.create_log.assert_not_called()


def test_enqueue_semantic_scoring_skips_when_already_queued():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = [
        SimpleNamespace(status=TaskStatus.QUEUED),
    ]

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.create_log.assert_not_called()
        semantic_task_mock.apply_async.assert_not_called()


def test_enqueue_semantic_scoring_creates_log_and_dispatches():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring, SEMANTIC_SCORE_TASK_TYPE

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = []
    created_log = SimpleNamespace(task_id=str(uuid4()))
    task_log_service.create_log.return_value = created_log

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.create_log.assert_called_once()
        create_log_kwargs = task_log_service.create_log.call_args.kwargs
        assert create_log_kwargs["task_type"] == SEMANTIC_SCORE_TASK_TYPE
        assert create_log_kwargs["campaign_candidate_id"] == campaign_candidate.id
        assert isinstance(create_log_kwargs["task_id"], str)

        semantic_task_mock.apply_async.assert_called_once_with(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=created_log.task_id,
        )


def test_enqueue_semantic_scoring_swallows_apply_async_failure():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = MagicMock()
    task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = []
    task_log_service.create_log.return_value = SimpleNamespace(task_id=str(uuid4()))

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        semantic_task_mock.apply_async.side_effect = Exception("broker unreachable")

        # Must not raise.
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)
