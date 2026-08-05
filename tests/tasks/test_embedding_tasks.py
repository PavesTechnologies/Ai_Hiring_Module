from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from celery.exceptions import Retry

from app.models.async_tasks import TaskStatus
from app.models.config import CBState
from app.models.pipeline import AIEvaluationStatus

TASKS_MODULE = "app.tasks.embedding_tasks"


def _make_resume(parsed_json=None):
    return SimpleNamespace(id=uuid4(), candidate_id=uuid4(), parsed_json=parsed_json)


def _make_model_version(model_name="all-MiniLM-L6-v2", model_version="v1"):
    return SimpleNamespace(id=uuid4(), model_name=model_name, model_version=model_version)


_PARSED_JSON = {
    "skills": ["Python", "SQL"],
    "work_experience": [{"title": "Engineer", "company": "Acme", "start_date": "2019", "end_date": "2022"}],
    "education": [{"degree": "Bachelor's", "field": "CS", "institution": "State University"}],
}


class _Harness:
    """Mirrors the _Harness pattern already established in test_email_tasks.py / test_deterministic_scoring_tasks.py."""

    def __init__(self):
        self.resume_repo = MagicMock()
        self.resume_repo.get_embedding_by_hash.return_value = None
        self.resume_repo.get_active_embedding_model_version.return_value = _make_model_version()
        # create_resume_embedding now returns (row, was_created) - defaults
        # to the "no race" case; tests that care about the race path
        # override this explicitly.
        self.resume_repo.create_resume_embedding.return_value = (MagicMock(), True)
        self.config_repo = MagicMock()
        self.config_repo.get_configs_by_keys.return_value = {}
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        # Real CeleryTaskLog objects flow through create()/update() unchanged,
        # so assertions can inspect status/output_summary/error_message
        # directly on the same object the task mutated. retry_count is
        # normalized to 0 here because it's only ever populated by
        # SQLAlchemy's real default=0 at flush/refresh time - a real
        # CeleryTaskLogRepository.create() would do the same before this
        # mock stands in for it.
        def _create(log):
            log.retry_count = log.retry_count or 0
            return log

        self.task_log_repo.create.side_effect = _create
        self.task_log_repo.update.side_effect = lambda log: log
        self.dead_letter_queue_repo = MagicMock()
        self.embedding_service_instance = MagicMock()
        self.embedding_service_instance.generate_embeddings.return_value = [[0.1] * 384]
        self.campaign_candidate_repo = MagicMock()
        self.campaign_candidate_repo.get_by_resume_id.return_value = []
        self.cb_repo = MagicMock()
        # Default: circuit CLOSED, so every existing test (none of which
        # care about the circuit breaker) proceeds straight through to the
        # embedding-service call exactly as before this feature existed.
        self.cb_repo.get_or_create.return_value = SimpleNamespace(state=CBState.CLOSED, retry_after=None)
        self.cb_repo.transition_to_half_open_if_due.return_value = SimpleNamespace(
            state=CBState.CLOSED, retry_after=None,
        )

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
            patch(f"{TASKS_MODULE}.EmbeddingService", return_value=self.embedding_service_instance),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CircuitBreakerRepository", return_value=self.cb_repo),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_generates_new_embedding_when_no_dedup_match():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.embedding_service_instance.generate_embeddings.assert_called_once()
        called_texts, = h.embedding_service_instance.generate_embeddings.call_args.args
        assert len(called_texts) == 1
        assert "Skills: Python, SQL" in called_texts[0]
        assert h.embedding_service_instance.generate_embeddings.call_args.kwargs["batch_size"] == 32

        h.resume_repo.create_resume_embedding.assert_called_once()
        create_kwargs = h.resume_repo.create_resume_embedding.call_args.kwargs
        assert create_kwargs["resume_id"] == resume.id
        assert create_kwargs["candidate_id"] == resume.candidate_id
        assert create_kwargs["embedding"] == [0.1] * 384
        h.resume_repo.commit.assert_called_once()

        assert h.task_log_repo.update.call_args is not None
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert "VECTOR_GENERATED" in task_log.output_summary


def test_reuses_existing_vector_on_dedup_hit():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        existing = SimpleNamespace(embedding=[0.9] * 384, is_anonymized=True, is_talent_pool_eligible=False)
        h.resume_repo.get_embedding_by_hash.return_value = existing

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.embedding_service_instance.generate_embeddings.assert_not_called()
        h.resume_repo.create_resume_embedding.assert_called_once()
        create_kwargs = h.resume_repo.create_resume_embedding.call_args.kwargs
        assert create_kwargs["embedding"] == [0.9] * 384
        # T4: is_anonymized is a property of the text/vector itself, so it
        # is still copied from the matched row.
        assert create_kwargs["is_anonymized"] is True
        # Talent Pool Eligibility: is_talent_pool_eligible is deliberately
        # NEVER copied from a different candidate's matched row (that
        # would let one candidate's disqualification leak onto an
        # unrelated candidate who just happens to share anonymised text) -
        # omitted here entirely so create_resume_embedding's own default
        # (True) applies; the daily reconciliation task is what corrects
        # this candidate's own eligibility afterward, never this dedup path.
        assert "is_talent_pool_eligible" not in create_kwargs

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert "VECTOR_REUSED" in task_log.output_summary


def test_reports_reused_when_fresh_generate_branch_loses_the_insert_race():
    """
    uq_resume_embeddings_resume_model_version is the final backstop even on
    the "no dedup-by-hash match" path: two concurrent workers can both miss
    the hash-based dedup check, both generate, then race on the same
    (resume_id, embedding_model_version_id) insert. The loser's
    create_resume_embedding returns was_created=False - the summary must
    say VECTOR_REUSED, never VECTOR_GENERATED, since no second row exists.
    """
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.resume_repo.create_resume_embedding.return_value = (MagicMock(), False)

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.embedding_service_instance.generate_embeddings.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS
        assert "VECTOR_REUSED" in task_log.output_summary
        assert "VECTOR_GENERATED" not in task_log.output_summary


def test_dead_letters_when_anonymization_verification_fails():
    """
    Anonymisation-verification failures must go through the same
    dead-letter path as every other non-retryable failure - not a bare
    mark_failure with no DLQ row, which would make them invisible to
    DLQ-based monitoring.
    """
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h, patch(
        f"{TASKS_MODULE}.verify_anonymized_text", return_value=(False, "Anonymised text appears to contain an email address."),
    ):
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.embedding_service_instance.generate_embeddings.assert_not_called()
        h.resume_repo.create_resume_embedding.assert_not_called()

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "EMBED_RESUME"
        assert create_kwargs["final_error_message"] == "Anonymised text appears to contain an email address."

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD
        assert task_log.error_message == "Anonymised text appears to contain an email address."


def test_dead_letters_when_parsed_json_missing():
    """Missing parsed_json raises ValueError -> PERMANENT -> dead-letter immediately, no retry."""
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=None)
        h.resume_repo.get_by_id.return_value = resume

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "EMBED_RESUME"
        assert create_kwargs["resume_id"] == resume.id

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD


def test_retries_on_transient_failure():
    """
    A ConnectionError classifies as TRANSIENT - must retry, never
    dead-letter, on the first attempt. Mirrors test_email_tasks.py's
    equivalent retry assertion: Celery's Task.retry(exc=ex) re-raises the
    original exception when called directly (no real worker context).
    """
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.resume_repo.get_active_embedding_model_version.side_effect = ConnectionError("db unreachable")

        with pytest.raises(ConnectionError):
            generate_resume_embedding_task(resume_id=str(resume.id))

        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY


def test_skips_duplicate_run_when_task_log_already_success():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        generate_resume_embedding_task(resume_id=str(uuid4()))

        h.resume_repo.get_by_id.assert_not_called()
        h.embedding_service_instance.generate_embeddings.assert_not_called()


def test_skips_gracefully_when_resume_no_longer_exists():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        h.resume_repo.get_by_id.return_value = None

        generate_resume_embedding_task(resume_id=str(uuid4()))

        h.embedding_service_instance.generate_embeddings.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_enqueue_skips_when_idempotency_key_already_present():
    from app.tasks.embedding_tasks import _enqueue_resume_embedding
    from app.services.celery_task_log_service import CeleryTaskLogService

    task_log_repo = MagicMock()
    task_log_repo.get_by_idempotency_key.return_value = SimpleNamespace(id=uuid4())
    task_log_service = CeleryTaskLogService(task_log_repo)

    with patch(f"{TASKS_MODULE}.generate_resume_embedding_task") as mocked_task:
        _enqueue_resume_embedding(MagicMock(), uuid4(), task_log_service)

        mocked_task.apply_async.assert_not_called()
        task_log_repo.create_if_new_idempotency_key.assert_not_called()


def test_enqueue_creates_log_and_dispatches_when_not_already_queued():
    from app.tasks.embedding_tasks import _enqueue_resume_embedding, EMBED_RESUME_TASK_TYPE
    from app.services.celery_task_log_service import CeleryTaskLogService

    task_log_repo = MagicMock()
    task_log_repo.get_by_idempotency_key.return_value = None
    task_log_repo.create_if_new_idempotency_key.side_effect = lambda log: (log, True)
    task_log_service = CeleryTaskLogService(task_log_repo)
    resume_id = uuid4()

    with patch(f"{TASKS_MODULE}.generate_resume_embedding_task") as mocked_task:
        _enqueue_resume_embedding(MagicMock(), resume_id, task_log_service)

        task_log_repo.create_if_new_idempotency_key.assert_called_once()
        created_log = task_log_repo.create_if_new_idempotency_key.call_args.args[0]
        assert created_log.task_type == EMBED_RESUME_TASK_TYPE
        assert created_log.resume_id == resume_id
        assert created_log.idempotency_key == f"{EMBED_RESUME_TASK_TYPE}:{resume_id}"
        task_log_repo.commit.assert_called_once()

        mocked_task.apply_async.assert_called_once()
        assert mocked_task.apply_async.call_args.kwargs["kwargs"] == {"resume_id": str(resume_id)}


def test_enqueue_skips_apply_async_when_insert_loses_the_idempotency_race():
    """
    uq_celery_task_log_idempotency_key is the DB-level backstop: two
    concurrent callers can both pass the pre-check above before either
    commits. When create_if_new_idempotency_key reports was_created=False
    (another caller already won), apply_async must never fire a second
    time for the same resume.
    """
    from app.tasks.embedding_tasks import _enqueue_resume_embedding
    from app.services.celery_task_log_service import CeleryTaskLogService

    task_log_repo = MagicMock()
    task_log_repo.get_by_idempotency_key.return_value = None
    winners_log = SimpleNamespace(id=uuid4())
    task_log_repo.create_if_new_idempotency_key.return_value = (winners_log, False)
    task_log_service = CeleryTaskLogService(task_log_repo)

    with patch(f"{TASKS_MODULE}.generate_resume_embedding_task") as mocked_task:
        _enqueue_resume_embedding(MagicMock(), uuid4(), task_log_service)

        mocked_task.apply_async.assert_not_called()


def test_batch_size_falls_back_to_default_on_invalid_config_value():
    """
    A malformed EMBEDDING_BATCH_SIZE must never propagate as a ValueError -
    that would classify PERMANENT and dead-letter every EMBED_RESUME run
    platform-wide until the config is fixed. It must log a warning and fall
    back to the default instead.
    """
    from app.tasks.embedding_tasks import _read_embedding_batch_size, _DEFAULT_EMBEDDING_BATCH_SIZE

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"EMBEDDING_BATCH_SIZE": "not-a-number"}

    assert _read_embedding_batch_size(config_repo) == _DEFAULT_EMBEDDING_BATCH_SIZE


def test_batch_size_uses_configured_value_when_valid():
    from app.tasks.embedding_tasks import _read_embedding_batch_size

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"EMBEDDING_BATCH_SIZE": "64"}

    assert _read_embedding_batch_size(config_repo) == 64


def test_batch_size_uses_default_when_unset():
    from app.tasks.embedding_tasks import _read_embedding_batch_size, _DEFAULT_EMBEDDING_BATCH_SIZE

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {}

    assert _read_embedding_batch_size(config_repo) == _DEFAULT_EMBEDDING_BATCH_SIZE


# ----------------------------------------------------------------------
# M08-E02: after a resume embedding commits (new or reused), this task
# must trigger any campaign_candidate for that resume that already passed
# deterministic screening but was left un-scored semantically because
# _enqueue_deterministic_scoring races ahead of _enqueue_resume_embedding
# (see resume_processing_tasks.py) - see
# trigger_pending_semantic_scoring_for_resume's own docstring for the
# full race explanation.
# ----------------------------------------------------------------------

def test_triggers_pending_semantic_scoring_after_embedding_commits():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume

        with patch(
            "app.tasks.semantic_scoring_tasks.trigger_pending_semantic_scoring_for_resume",
        ) as trigger_mock:
            generate_resume_embedding_task(resume_id=str(resume.id))

            trigger_mock.assert_called_once()
            call_args = trigger_mock.call_args.args
            assert call_args[1] == resume.id
            h.resume_repo.commit.assert_called_once()

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_pending_semantic_scoring_trigger_failure_never_crashes_embedding_task():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume

        with patch(
            "app.tasks.semantic_scoring_tasks.trigger_pending_semantic_scoring_for_resume",
            side_effect=Exception("boom"),
        ):
            # Must not raise - the embedding itself already committed
            # successfully and must still be reported as a success.
            generate_resume_embedding_task(resume_id=str(resume.id))


# ----------------------------------------------------------------------
# Resilient retry: circuit breaker gating before the embedding-service
# call, HTTP-status-aware classification (429/500/503/400), config-driven
# MAX_EMBED_RETRY_COUNT, and MANUAL_REVIEW on permanent failure.
# ----------------------------------------------------------------------

class _HTTPStatusError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


def test_circuit_open_reschedules_without_calling_embedding_service():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        future = datetime.now(timezone.utc) + timedelta(seconds=42)
        h.cb_repo.transition_to_half_open_if_due.return_value = SimpleNamespace(
            state=CBState.OPEN, retry_after=future,
        )

        with pytest.raises(Exception):  # Celery's Retry exception propagates when called directly
            generate_resume_embedding_task(resume_id=str(resume.id))

        h.embedding_service_instance.generate_embeddings.assert_not_called()
        h.cb_repo.increment_failure.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY
        # The circuit-breaker-open reschedule must never count against
        # MAX_EMBED_RETRY_COUNT's own counter.
        assert task_log.retry_count == 0


def test_dedup_hit_never_checks_circuit_breaker():
    """A content-hash dedup hit never calls the model, so an OPEN circuit must not block it."""
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.resume_repo.get_embedding_by_hash.return_value = SimpleNamespace(
            embedding=[0.5] * 384, is_anonymized=True, is_talent_pool_eligible=True,
        )
        h.cb_repo.transition_to_half_open_if_due.return_value = SimpleNamespace(state=CBState.OPEN, retry_after=None)

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.cb_repo.transition_to_half_open_if_due.assert_not_called()
        h.embedding_service_instance.generate_embeddings.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_successful_embedding_call_resets_circuit_breaker():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.cb_repo.reset.assert_called_once_with("EMBEDDING_SERVICE")


def test_rate_limited_failure_retries_using_retry_after_header():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.embedding_service_instance.generate_embeddings.side_effect = _HTTPStatusError(
            429, headers={"Retry-After": "17"},
        )

        with patch(f"{TASKS_MODULE}.generate_resume_embedding_task.retry") as retry_mock:
            retry_mock.side_effect = Retry("retry called")
            with pytest.raises(Retry):
                generate_resume_embedding_task(resume_id=str(resume.id))

            assert retry_mock.call_args.kwargs["countdown"] == 17.0

        h.cb_repo.increment_failure.assert_called_once_with("EMBEDDING_SERVICE")
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY
        assert task_log.retry_count == 1


def test_server_error_failure_uses_config_driven_exponential_backoff():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.embedding_service_instance.generate_embeddings.side_effect = _HTTPStatusError(503)
        h.config_repo.get_configs_by_keys.return_value = {
            "MAX_EMBED_RETRY_COUNT": "4",
            "EMBED_RETRY_BASE_DELAY_SECONDS": "30",
            "EMBED_RETRY_MAX_DELAY_SECONDS": "240",
        }

        with patch(f"{TASKS_MODULE}.generate_resume_embedding_task.retry") as retry_mock:
            retry_mock.side_effect = Retry("retry called")
            with pytest.raises(Retry):
                generate_resume_embedding_task(resume_id=str(resume.id))

            assert retry_mock.call_args.kwargs["countdown"] == 30


def test_permanent_400_failure_dead_letters_immediately_and_sets_manual_review():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        cc = SimpleNamespace(id=uuid4(), ai_evaluation_status=None)
        h.campaign_candidate_repo.get_by_resume_id.return_value = [cc]
        h.embedding_service_instance.generate_embeddings.side_effect = _HTTPStatusError(400)

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD
        assert cc.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW


def test_retries_exhausted_dead_letters_and_sets_manual_review_for_all_candidates():
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        cc_a = SimpleNamespace(id=uuid4(), ai_evaluation_status=None)
        cc_b = SimpleNamespace(id=uuid4(), ai_evaluation_status=None)
        h.campaign_candidate_repo.get_by_resume_id.return_value = [cc_a, cc_b]
        h.embedding_service_instance.generate_embeddings.side_effect = _HTTPStatusError(503)
        h.config_repo.get_configs_by_keys.return_value = {"MAX_EMBED_RETRY_COUNT": "4"}
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(
            status=TaskStatus.RUNNING, retry_count=4, queued_at=None, id=uuid4(), started_at=None,
        )

        generate_resume_embedding_task(resume_id=str(resume.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD
        assert cc_a.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW
        assert cc_b.ai_evaluation_status == AIEvaluationStatus.MANUAL_REVIEW


def test_upstream_db_error_never_touches_circuit_breaker():
    """
    A ConnectionError from get_active_embedding_model_version() happens
    before the embedding-service call is ever reached - unrelated to
    EMBEDDING_SERVICE's own health, so it must never increment the
    circuit breaker (it goes through the legacy generic-classifier path
    instead, unchanged from before this feature existed).
    """
    from app.tasks.embedding_tasks import generate_resume_embedding_task

    with _Harness() as h:
        resume = _make_resume(parsed_json=_PARSED_JSON)
        h.resume_repo.get_by_id.return_value = resume
        h.resume_repo.get_active_embedding_model_version.side_effect = ConnectionError("db unreachable")

        with pytest.raises(ConnectionError):
            generate_resume_embedding_task(resume_id=str(resume.id))

        h.cb_repo.increment_failure.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY
