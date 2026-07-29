from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus

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

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
            patch(f"{TASKS_MODULE}.EmbeddingService", return_value=self.embedding_service_instance),
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
        # T4: is_anonymized/is_talent_pool_eligible must be copied from the
        # matched row, not silently re-defaulted to True/True.
        assert create_kwargs["is_anonymized"] is True
        assert create_kwargs["is_talent_pool_eligible"] is False

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
