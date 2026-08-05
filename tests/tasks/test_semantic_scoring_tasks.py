import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage, RejectionLayer
from app.services.celery_task_log_service import CeleryTaskLogService

TASKS_MODULE = "app.tasks.semantic_scoring_tasks"


def _breakdown(passed=True, similarity=0.8, threshold=0.65, score_clamped_to_zero=False):
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
        "computation_duration_ms": 5,
        "score_clamped_to_zero": score_clamped_to_zero,
        "score_clamp_reason": "SCORE_CLAMPED_TO_ZERO" if score_clamped_to_zero else None,
        "semantic_check": {"jd_embedding_id": str(uuid4()), "resume_embedding_id": str(uuid4())},
    }


class _Harness:
    """Mirrors the _Harness pattern in test_deterministic_scoring_tasks.py / test_embedding_tasks.py."""

    def __init__(self):
        self.campaign_candidate_repo = MagicMock()
        self.campaign_repo = MagicMock()
        self.resume_repo = MagicMock()
        self.jd_repo = MagicMock()
        self.candidate_rejection_repo = MagicMock()
        self.allowed_transition_repo = MagicMock()
        self.allowed_transition_repo.is_transition_allowed.return_value = True
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        # Default: no queued AI_EVALUATE (or other) logs, so
        # _cancel_downstream_ai_evaluation and _queue_ai_evaluate_if_not_duplicate
        # (both run for real - fully generic and safe against mocks) see a
        # clean slate. create() returns the log object it was given, so
        # tests can inspect what _queue_ai_evaluate_if_not_duplicate created.
        self.task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
        self.task_log_repo.create.side_effect = lambda log: log
        # Task 536: both embeddings exist and share a model version by
        # default, so every pre-existing test (which doesn't care about
        # this pre-flight validation) passes straight through to the
        # scoring service exactly as before. Tests targeting the new
        # validation paths override these explicitly.
        self.embedding_model_version_id = uuid4()
        self.resume_repo.get_embedding.return_value = SimpleNamespace(
            id=uuid4(), embedding_model_version_id=self.embedding_model_version_id,
        )
        self.jd_repo.get_embedding_by_jd_id.return_value = SimpleNamespace(
            id=uuid4(), embedding_model_version_id=self.embedding_model_version_id,
        )
        self.audit_service_instance = MagicMock()
        self.scoring_service_instance = MagicMock()
        self.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown()
        self.dead_letter_queue_repo = MagicMock()
        # Story 542: _queue_rejection_email is imported from
        # deterministic_scoring_tasks (a real function that would otherwise
        # try a real EmailTemplateRepository query + Celery apply_async) -
        # patched at the name bound in THIS module by that import, same
        # convention as test_deterministic_scoring_tasks.py's own
        # send_candidate_email_task_mock.
        self.queue_rejection_email_mock = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.CandidateRejectionRepository", return_value=self.candidate_rejection_repo),
            patch(f"{TASKS_MODULE}.AllowedTransitionRepository", return_value=self.allowed_transition_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.SemanticScoringService", return_value=self.scoring_service_instance),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
            patch(f"{TASKS_MODULE}._queue_rejection_email", self.queue_rejection_email_mock),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _make_campaign_candidate(
    campaign_id=None, resume_id=None, pipeline_stage=PipelineStage.SCREENING,
    deterministic_passed=True, candidate_id=None, semantic_score_breakdown=None,
):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id or uuid4(), resume_id=resume_id or uuid4(),
        candidate_id=candidate_id or uuid4(), pipeline_stage=pipeline_stage,
        deterministic_passed=deterministic_passed,
        semantic_score_breakdown=semantic_score_breakdown,
    )


def _make_campaign(status=CampaignStatus.ACTIVE, jd_id=None, semantic_threshold=0.65):
    return SimpleNamespace(id=uuid4(), status=status, jd_id=jd_id or uuid4(), semantic_threshold=semantic_threshold)


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


# ----------------------------------------------------------------------
# Explicit missing-resume-embedding skip - checked BEFORE ever calling
# SemanticScoringService, so this never even reaches the retry/dead-letter
# path above: a graceful, immediate skip (MANUAL_REVIEW + a SEMANTIC
# candidate_rejections row), never a retry, never an automatic rejection
# of the candidate (pipeline_stage is left untouched).
# ----------------------------------------------------------------------

def test_missing_resume_embedding_skips_gracefully_without_scoring():
    from app.models.pipeline import AIEvaluationStatus
    from app.tasks.semantic_scoring_tasks import MISSING_RESUME_EMBEDDING_REASON, calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_embedding.return_value = None
        h.candidate_rejection_repo.get_by_campaign_candidate_id.return_value = []

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        assert cc.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW
        # Never an automatic rejection - pipeline_stage must be untouched.
        assert cc.pipeline_stage == PipelineStage.SCREENING

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_layer == RejectionLayer.SEMANTIC
        assert rejection.rejection_reason == MISSING_RESUME_EMBEDDING_REASON

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert json.loads(task_log.output_summary)["reason"] == MISSING_RESUME_EMBEDDING_REASON


def test_missing_resume_embedding_skip_is_idempotent():
    """A second run before the embedding is ready must not insert a duplicate rejection row."""
    from app.tasks.semantic_scoring_tasks import MISSING_RESUME_EMBEDDING_REASON, calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_embedding.return_value = None
        h.candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [
            SimpleNamespace(rejection_layer=RejectionLayer.SEMANTIC, rejection_reason=MISSING_RESUME_EMBEDDING_REASON),
        ]

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


# ----------------------------------------------------------------------
# Task 536: JD embedding pre-flight (flat 60s/3-retry policy, distinct
# from the exponential _SEMANTIC_SCORE_RETRY_POLICY used for unexpected
# exceptions) and resume/JD embedding_model_version_id comparison - both
# checked, and logged into celery_task_log, before the similarity
# calculation itself ever runs.
# ----------------------------------------------------------------------

def test_jd_embedding_missing_retries_on_flat_60s_interval():
    from celery.exceptions import Retry

    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.jd_repo.get_embedding_by_jd_id.return_value = None

        with pytest.raises(Retry):
            calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY


def test_jd_embedding_still_missing_after_max_retries_routes_to_manual_review():
    from app.models.pipeline import AIEvaluationStatus
    from app.tasks.semantic_scoring_tasks import JD_EMBEDDING_NOT_FOUND_REASON, calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.jd_repo.get_embedding_by_jd_id.return_value = None

        calculate_semantic_score_task.push_request(retries=3, id=str(uuid4()))
        try:
            calculate_semantic_score_task(campaign_candidate_id=str(cc.id))
        finally:
            calculate_semantic_score_task.pop_request()

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        assert cc.semantic_score is None
        assert cc.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW
        # Never an automatic rejection - pipeline_stage must be untouched.
        assert cc.pipeline_stage == PipelineStage.SCREENING

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert json.loads(task_log.output_summary)["reason"] == JD_EMBEDDING_NOT_FOUND_REASON


def test_model_version_mismatch_routes_to_manual_review_without_scoring():
    from app.models.pipeline import AIEvaluationStatus
    from app.tasks.semantic_scoring_tasks import MODEL_VERSION_MISMATCH_REASON, calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())
        h.jd_repo.get_embedding_by_jd_id.return_value = SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_not_called()
        assert cc.semantic_score is None
        assert cc.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW
        assert cc.pipeline_stage == PipelineStage.SCREENING

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert json.loads(task_log.output_summary)["reason"] == MODEL_VERSION_MISMATCH_REASON


def test_validations_logged_to_celery_task_log_before_scoring_runs():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        # task_log is the same mutable object across every update() call, so
        # inspecting call_args after the fact would only ever show its final
        # state - capture each output_summary string (immutable) as it's
        # written instead.
        recorded_summaries = []

        def _capture(log):
            if log.output_summary:
                recorded_summaries.append(log.output_summary)
            return log
        h.task_log_repo.update.side_effect = _capture

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        validation_summaries = [s for s in recorded_summaries if "validations" in s]
        assert len(validation_summaries) >= 1
        validations = json.loads(validation_summaries[0])["validations"]
        assert validations == {
            "resume_embedding_found": True,
            "jd_embedding_found": True,
            "model_versions_match": True,
        }
        # The pre-flight log write happened before scoring - and scoring still ran.
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_called_once()


def test_campaign_semantic_threshold_is_passed_to_the_scoring_service():
    """
    Story 541: hiring_campaigns.semantic_threshold - never a global
    platform_config value - drives the pass/fail decision.
    """
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign(semantic_threshold=0.42)
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.assert_called_once_with(
            cc.id, campaign.jd_id, cc.resume_id, semantic_threshold=0.42,
        )


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


# ----------------------------------------------------------------------
# Story 541 - semantic PASS queues AI_EVALUATE (bookkeeping placeholder
# only, no real Celery task exists yet); REJECT never does.
# ----------------------------------------------------------------------

def test_semantic_pass_queues_ai_evaluate_placeholder():
    from app.tasks.deterministic_scoring_tasks import AI_EVALUATE_TASK_TYPE
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=True)

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        ai_evaluate_logs = [
            call.args[0] for call in h.task_log_repo.create.call_args_list
            if call.args[0].task_type == AI_EVALUATE_TASK_TYPE
        ]
        assert len(ai_evaluate_logs) == 1
        created_log = ai_evaluate_logs[0]
        assert created_log.campaign_candidate_id == cc.id
        assert created_log.status == TaskStatus.QUEUED


def test_semantic_pass_does_not_queue_duplicate_ai_evaluate_when_already_queued():
    from app.tasks.deterministic_scoring_tasks import AI_EVALUATE_TASK_TYPE
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=True)

        def _get_by_type(_campaign_candidate_id, task_type):
            if task_type == AI_EVALUATE_TASK_TYPE:
                return [SimpleNamespace(status=TaskStatus.QUEUED)]
            return []
        h.task_log_repo.get_by_campaign_candidate_and_task_type.side_effect = _get_by_type

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        created_task_types = [call.args[0].task_type for call in h.task_log_repo.create.call_args_list]
        assert AI_EVALUATE_TASK_TYPE not in created_task_types


def test_semantic_rejection_never_queues_ai_evaluate():
    from app.tasks.deterministic_scoring_tasks import AI_EVALUATE_TASK_TYPE
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=False)

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        created_task_types = [call.args[0].task_type for call in h.task_log_repo.create.call_args_list]
        assert AI_EVALUATE_TASK_TYPE not in created_task_types


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


# ----------------------------------------------------------------------
# Story 542 - the existing rejection email is reused (never a separate
# semantic-specific implementation), queued only after the transaction
# commits, and only when the REJECTED transition actually succeeded.
# ----------------------------------------------------------------------

def test_semantic_rejection_queues_the_existing_rejection_email():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=False)

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.queue_rejection_email_mock.assert_called_once_with(h.campaign_candidate_repo.db, cc)


def test_semantic_pass_never_queues_a_rejection_email():
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=True)

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        h.queue_rejection_email_mock.assert_not_called()


def test_semantic_rejection_never_queues_email_when_transition_blocked():
    """A blocked REJECTED transition must not send a rejection email for a candidate who never actually moved."""
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(passed=False)
        h.allowed_transition_repo.is_transition_allowed.return_value = False

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        assert cc.pipeline_stage == PipelineStage.SCREENING
        h.queue_rejection_email_mock.assert_not_called()


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


def test_computation_duration_recorded_on_celery_task_log():
    """Task 538: duration_ms persisted on the same task_log row mark_success() writes."""
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        breakdown = _breakdown(passed=True)
        breakdown["computation_duration_ms"] = 42
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = breakdown

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.duration_ms == 42


def test_score_clamped_marker_recorded_in_celery_task_log_output_summary():
    """Story 540: literal 'SCORE_CLAMPED_TO_ZERO' marker lands in celery_task_log.output_summary."""
    from app.tasks.semantic_scoring_tasks import calculate_semantic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.scoring_service_instance.calculate_and_store_semantic_score_breakdown.return_value = _breakdown(
            passed=True, similarity=0.0, score_clamped_to_zero=True,
        )

        calculate_semantic_score_task(campaign_candidate_id=str(cc.id))

        task_log = h.task_log_repo.update.call_args.args[0]
        summary = json.loads(task_log.output_summary)
        assert summary["score_clamped_to_zero"] is True
        assert summary["score_clamp_reason"] == "SCORE_CLAMPED_TO_ZERO"


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


def _make_enqueue_task_log_service(get_by_idempotency_key_return=None):
    task_log_service = MagicMock()
    task_log_service.repository.db = MagicMock()
    task_log_service.repository.get_by_idempotency_key.return_value = get_by_idempotency_key_return
    return task_log_service


def test_enqueue_semantic_scoring_enqueues_embed_resume_when_resume_has_no_embedding():
    """
    Task 537: a missing resume embedding must not stall the candidate
    forever - EMBED_RESUME is enqueued as a fallback so
    trigger_pending_semantic_scoring_for_resume auto-triggers this same
    candidate once that embedding actually completes.
    """
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = None
    task_log_service = _make_enqueue_task_log_service()

    with patch("app.tasks.embedding_tasks._enqueue_resume_embedding") as enqueue_embed_resume_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.create_log.assert_not_called()
        enqueue_embed_resume_mock.assert_called_once_with(
            task_log_service.repository.db, campaign_candidate.resume_id, task_log_service,
        )


def test_enqueue_semantic_scoring_embed_resume_fallback_failure_is_swallowed():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = None
    task_log_service = _make_enqueue_task_log_service()

    with patch("app.tasks.embedding_tasks._enqueue_resume_embedding") as enqueue_embed_resume_mock:
        enqueue_embed_resume_mock.side_effect = Exception("broker unreachable")

        # Must not raise.
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)


def test_enqueue_semantic_scoring_skips_when_already_queued():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = _make_enqueue_task_log_service(
        get_by_idempotency_key_return=SimpleNamespace(status=TaskStatus.QUEUED),
    )

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.create_log.assert_not_called()
        task_log_service.repository.update.assert_not_called()
        semantic_task_mock.apply_async.assert_not_called()


def test_enqueue_semantic_scoring_skips_when_already_successful():
    """Prevent duplicate semantic task creation if one has already succeeded, not just QUEUED/RUNNING."""
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = _make_enqueue_task_log_service(
        get_by_idempotency_key_return=SimpleNamespace(status=TaskStatus.SUCCESS),
    )

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.create_log.assert_not_called()
        semantic_task_mock.apply_async.assert_not_called()


def test_enqueue_semantic_scoring_creates_log_with_hash_idempotency_key_and_dispatches():
    from app.tasks.semantic_scoring_tasks import (
        SEMANTIC_SCORE_TASK_TYPE,
        _enqueue_semantic_scoring,
        _semantic_score_idempotency_key,
    )

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = _make_enqueue_task_log_service(get_by_idempotency_key_return=None)
    created_log = SimpleNamespace(task_id=str(uuid4()))
    task_log_service.repository.create_if_new_idempotency_key.return_value = (created_log, True)
    jd_id = uuid4()

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo, jd_id=jd_id)

        task_log_service.repository.create_if_new_idempotency_key.assert_called_once()
        inserted_log = task_log_service.repository.create_if_new_idempotency_key.call_args.args[0]
        assert inserted_log.task_type == SEMANTIC_SCORE_TASK_TYPE
        assert inserted_log.campaign_candidate_id == campaign_candidate.id
        assert inserted_log.jd_id == jd_id
        assert inserted_log.idempotency_key == _semantic_score_idempotency_key(campaign_candidate.id)

        semantic_task_mock.apply_async.assert_called_once_with(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=created_log.task_id,
        )


def test_enqueue_semantic_scoring_loses_insert_race_and_finds_already_queued():
    """Two concurrent callers both see no existing row - the loser must not double-dispatch."""
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = _make_enqueue_task_log_service(get_by_idempotency_key_return=None)
    winners_log = SimpleNamespace(status=TaskStatus.QUEUED, task_id=str(uuid4()))
    task_log_service.repository.create_if_new_idempotency_key.return_value = (winners_log, False)

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        semantic_task_mock.apply_async.assert_not_called()


def test_enqueue_semantic_scoring_retriggers_existing_row_after_terminal_failure():
    """
    A prior SEMANTIC_SCORE attempt for this candidate reached a terminal
    FAILURE - a re-trigger (HR override, recovery scan) must reuse and
    reset that SAME idempotency-keyed row (a second insert with the same
    key would violate uq_celery_task_log_idempotency_key), not skip it.
    """
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    failed_log = SimpleNamespace(status=TaskStatus.FAILURE, task_id=str(uuid4()))
    task_log_service = _make_enqueue_task_log_service(get_by_idempotency_key_return=failed_log)
    task_log_service.repository.update.side_effect = lambda log: log

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        task_log_service.repository.create_if_new_idempotency_key.assert_not_called()
        task_log_service.repository.update.assert_called_once_with(failed_log)
        assert failed_log.status == TaskStatus.QUEUED
        semantic_task_mock.apply_async.assert_called_once_with(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=failed_log.task_id,
        )


def test_enqueue_semantic_scoring_swallows_apply_async_failure():
    from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring

    campaign_candidate = _make_campaign_candidate_for_enqueue()
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    task_log_service = _make_enqueue_task_log_service(get_by_idempotency_key_return=None)
    task_log_service.repository.create_if_new_idempotency_key.return_value = (
        SimpleNamespace(task_id=str(uuid4())), True,
    )

    with patch(f"{TASKS_MODULE}.calculate_semantic_score_task") as semantic_task_mock:
        semantic_task_mock.apply_async.side_effect = Exception("broker unreachable")

        # Must not raise.
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)


# ----------------------------------------------------------------------
# trigger_pending_semantic_scoring_for_resume - closes the race between
# EMBED_RESUME and the deterministic-scoring auto-enqueue:
# _enqueue_deterministic_scoring is dispatched before
# _enqueue_resume_embedding (see resume_processing_tasks.py), so
# _enqueue_semantic_scoring's own resume-embedding prerequisite check can
# (and, in the common single-resume-upload flow, often does) skip
# enqueueing before the resume's embedding exists yet. Called from
# generate_resume_embedding_task right after it commits a new/reused
# embedding, this catches any campaign_candidate for that resume left in
# that state and offers it to _enqueue_semantic_scoring again (now that
# the embedding exists).
# ----------------------------------------------------------------------

class _TriggerHarness:
    def __init__(self):
        self.campaign_candidate_repo = MagicMock()
        self.resume_repo = MagicMock()
        self.task_log_repo = MagicMock()
        self.enqueue_semantic_scoring_mock = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}._enqueue_semantic_scoring", self.enqueue_semantic_scoring_mock),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_trigger_pending_enqueues_deterministic_passed_unscored_candidates():
    from app.tasks.semantic_scoring_tasks import trigger_pending_semantic_scoring_for_resume

    resume_id = uuid4()
    with _TriggerHarness() as h:
        pending_cc = _make_campaign_candidate(deterministic_passed=True, semantic_score_breakdown=None)
        h.campaign_candidate_repo.get_by_resume_id.return_value = [pending_cc]

        trigger_pending_semantic_scoring_for_resume(MagicMock(), resume_id)

        h.campaign_candidate_repo.get_by_resume_id.assert_called_once_with(resume_id)
        h.enqueue_semantic_scoring_mock.assert_called_once()
        call_args = h.enqueue_semantic_scoring_mock.call_args.args
        assert call_args[0] is pending_cc
        assert call_args[2] is h.resume_repo


def test_trigger_pending_skips_candidates_not_deterministic_passed():
    from app.tasks.semantic_scoring_tasks import trigger_pending_semantic_scoring_for_resume

    with _TriggerHarness() as h:
        not_passed_cc = _make_campaign_candidate(deterministic_passed=False, semantic_score_breakdown=None)
        h.campaign_candidate_repo.get_by_resume_id.return_value = [not_passed_cc]

        trigger_pending_semantic_scoring_for_resume(MagicMock(), uuid4())

        h.enqueue_semantic_scoring_mock.assert_not_called()


def test_trigger_pending_skips_candidates_already_scored():
    from app.tasks.semantic_scoring_tasks import trigger_pending_semantic_scoring_for_resume

    with _TriggerHarness() as h:
        already_scored_cc = _make_campaign_candidate(
            deterministic_passed=True, semantic_score_breakdown={"semantic_score": 0.9},
        )
        h.campaign_candidate_repo.get_by_resume_id.return_value = [already_scored_cc]

        trigger_pending_semantic_scoring_for_resume(MagicMock(), uuid4())

        h.enqueue_semantic_scoring_mock.assert_not_called()


def test_trigger_pending_enqueues_multiple_candidates_independently():
    """
    A resume reused across more than one campaign (get_by_resume_id can
    return more than one row) - each pending candidate must be offered to
    _enqueue_semantic_scoring independently, and one being already-scored/
    not-passed must never block the others.
    """
    from app.tasks.semantic_scoring_tasks import trigger_pending_semantic_scoring_for_resume

    with _TriggerHarness() as h:
        pending_a = _make_campaign_candidate(deterministic_passed=True, semantic_score_breakdown=None)
        already_scored_b = _make_campaign_candidate(
            deterministic_passed=True, semantic_score_breakdown={"semantic_score": 0.5},
        )
        h.campaign_candidate_repo.get_by_resume_id.return_value = [pending_a, already_scored_b]

        trigger_pending_semantic_scoring_for_resume(MagicMock(), uuid4())

        h.enqueue_semantic_scoring_mock.assert_called_once()
        assert h.enqueue_semantic_scoring_mock.call_args.args[0] is pending_a


# ----------------------------------------------------------------------
# recover_pending_semantic_scores - requirement 4's automatic recovery
# Beat task: re-queues SEMANTIC_SCORE for every campaign_candidate the
# repository query reports as "deterministic_passed, no semantic_score,
# resume embedding now exists."
# ----------------------------------------------------------------------

def test_recovery_task_enqueues_every_candidate_the_query_returns():
    from app.tasks.semantic_scoring_tasks import recover_pending_semantic_scores

    with _TriggerHarness() as h:
        cc_a = _make_campaign_candidate(deterministic_passed=True)
        cc_b = _make_campaign_candidate(deterministic_passed=True)
        h.campaign_candidate_repo.get_pending_semantic_score_with_ready_embedding.return_value = [cc_a, cc_b]

        recover_pending_semantic_scores()

        assert h.enqueue_semantic_scoring_mock.call_count == 2
        called_candidates = [call.args[0] for call in h.enqueue_semantic_scoring_mock.call_args_list]
        assert cc_a in called_candidates
        assert cc_b in called_candidates

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_recovery_task_no_op_when_nothing_pending():
    from app.tasks.semantic_scoring_tasks import recover_pending_semantic_scores

    with _TriggerHarness() as h:
        h.campaign_candidate_repo.get_pending_semantic_score_with_ready_embedding.return_value = []

        recover_pending_semantic_scores()

        h.enqueue_semantic_scoring_mock.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_recovery_task_marks_failure_on_unexpected_exception():
    from app.tasks.semantic_scoring_tasks import recover_pending_semantic_scores

    with _TriggerHarness() as h:
        h.campaign_candidate_repo.get_pending_semantic_score_with_ready_embedding.side_effect = RuntimeError("db down")

        # Must not raise - a scheduled Beat task failing loudly would show
        # up as a Celery-level error rather than clean task_log bookkeeping.
        recover_pending_semantic_scores()

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.FAILURE
