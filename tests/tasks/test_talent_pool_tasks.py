from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import TaskStatus
from app.models.pipeline import PipelineStage

TASKS_MODULE = "app.tasks.talent_pool_tasks"


def _make_candidate(is_pii_deleted=False, erasure_requested_at=None):
    return SimpleNamespace(id=uuid4(), is_pii_deleted=is_pii_deleted, erasure_requested_at=erasure_requested_at)


def _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, is_fraud_flagged=False, created_at=None):
    return SimpleNamespace(
        id=uuid4(), pipeline_stage=pipeline_stage, is_fraud_flagged=is_fraud_flagged,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_embedding(is_talent_pool_eligible=True):
    return SimpleNamespace(id=uuid4(), is_talent_pool_eligible=is_talent_pool_eligible)


class _Harness:
    def __init__(self):
        self.resume_repo = MagicMock()
        self.candidate_repo = MagicMock()
        self.campaign_candidate_repo = MagicMock()
        self.campaign_candidate_repo.get_by_candidate_id.return_value = []
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None

        def _create(log):
            log.retry_count = getattr(log, "retry_count", 0) or 0
            return log

        self.task_log_repo.create.side_effect = _create
        self.task_log_repo.update.side_effect = lambda log: log
        self.db = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=self.db),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.CandidateRepository", return_value=self.candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_no_correction_when_already_eligible_and_should_be():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=True)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_corrects_to_false_when_pii_deleted():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate(is_pii_deleted=True)
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=True)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_called_once_with(candidate_id, False)


def test_corrects_to_false_when_erasure_requested_even_if_pii_not_yet_deleted():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate(
            is_pii_deleted=False, erasure_requested_at=datetime.now(timezone.utc),
        )
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=True)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_called_once_with(candidate_id, False)


def test_corrects_to_false_when_latest_campaign_candidate_fraud_flagged_in_fraud_review():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_candidate_id.return_value = [
            _make_campaign_candidate(pipeline_stage=PipelineStage.FRAUD_REVIEW, is_fraud_flagged=True),
        ]
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=True)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_called_once_with(candidate_id, False)


def test_does_not_disqualify_on_fraud_review_without_fraud_flag():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_candidate_id.return_value = [
            _make_campaign_candidate(pipeline_stage=PipelineStage.FRAUD_REVIEW, is_fraud_flagged=False),
        ]
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=True)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_not_called()


def test_only_latest_campaign_candidate_considered_for_fraud():
    """An older FRAUD_REVIEW+flagged row must not disqualify if the LATEST row has since cleared."""
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        older = _make_campaign_candidate(
            pipeline_stage=PipelineStage.FRAUD_REVIEW, is_fraud_flagged=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        newer = _make_campaign_candidate(
            pipeline_stage=PipelineStage.SCREENING, is_fraud_flagged=False,
            created_at=datetime.now(timezone.utc),
        )
        h.campaign_candidate_repo.get_by_candidate_id.return_value = [older, newer]
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=False)]

        validate_talent_pool_eligibility()

        # Currently False but should now be True (latest row cleared) - corrected back.
        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_called_once_with(candidate_id, True)


def test_corrects_back_to_true_when_fraud_flag_cleared():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_candidate_id.return_value = [
            _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, is_fraud_flagged=False),
        ]
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=False)]

        validate_talent_pool_eligibility()

        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_called_once_with(candidate_id, True)


def test_never_restores_eligibility_once_pii_deleted_even_if_currently_false():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = _make_candidate(is_pii_deleted=True)
        h.resume_repo.get_embeddings_by_candidate.return_value = [_make_embedding(is_talent_pool_eligible=False)]

        validate_talent_pool_eligibility()

        # Already False and should stay False - no update needed.
        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_not_called()


def test_skips_orphaned_embedding_when_candidate_no_longer_exists():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        candidate_id = uuid4()
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.return_value = [candidate_id]
        h.candidate_repo.get_by_id.return_value = None

        validate_talent_pool_eligibility()

        h.resume_repo.get_embeddings_by_candidate.assert_not_called()
        h.resume_repo.set_talent_pool_eligibility_for_candidate.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_marks_failure_on_unexpected_exception():
    from app.tasks.talent_pool_tasks import validate_talent_pool_eligibility

    with _Harness() as h:
        h.resume_repo.get_distinct_candidate_ids_with_embeddings.side_effect = RuntimeError("db down")

        validate_talent_pool_eligibility()

        h.db.rollback.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.FAILURE
