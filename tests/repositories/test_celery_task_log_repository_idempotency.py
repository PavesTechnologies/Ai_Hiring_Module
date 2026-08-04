from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository


@contextmanager
def _reraising_savepoint():
    yield


def _repo_with_mock_db():
    db = MagicMock()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    return db, CeleryTaskLogRepository(db)


def _make_log(idempotency_key="EMBED_RESUME:some-resume-id"):
    return CeleryTaskLog(
        task_id="task-1", task_type="EMBED_RESUME", idempotency_key=idempotency_key, status=TaskStatus.QUEUED,
    )


def test_create_if_new_idempotency_key_returns_new_log_when_no_conflict():
    db, repo = _repo_with_mock_db()
    log = _make_log()

    result, was_created = repo.create_if_new_idempotency_key(log)

    assert was_created is True
    assert result is log
    db.add.assert_called_once_with(log)
    db.flush.assert_called_once()


def test_create_if_new_idempotency_key_falls_back_to_existing_row_on_integrity_error():
    """
    uq_celery_task_log_idempotency_key is the DB-level backstop for two
    concurrent enqueue calls racing on the same idempotency_key - the
    loser's flush raises IntegrityError, which must resolve to the
    winner's already-committed row instead of propagating, so the caller
    (_enqueue_resume_embedding) can skip its own apply_async.
    """
    db, repo = _repo_with_mock_db()
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    existing_log = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing_log

    result, was_created = repo.create_if_new_idempotency_key(_make_log())

    assert was_created is False
    assert result is existing_log


# ----------------------------------------------------------------------
# Resume-upload resilience: get_queued_dispatch_failed / claim_for_redispatch.
# ----------------------------------------------------------------------

def test_get_queued_dispatch_failed_filters_by_task_type_status_and_flag():
    db = MagicMock()
    repo = CeleryTaskLogRepository(db)
    matching_rows = [MagicMock(), MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = matching_rows

    result = repo.get_queued_dispatch_failed("RESUME_DOCUMENT_PROCESSING")

    assert result == matching_rows
    db.query.assert_called_once_with(CeleryTaskLog)


def test_claim_for_redispatch_returns_true_when_row_claimed():
    db = MagicMock()
    repo = CeleryTaskLogRepository(db)
    db.execute.return_value = MagicMock(rowcount=1)

    claimed = repo.claim_for_redispatch("some-log-id")

    assert claimed is True
    db.commit.assert_called_once()


def test_claim_for_redispatch_returns_false_when_already_claimed():
    """A concurrent recovery run (or an already-recovered row) must not be claimed twice."""
    db = MagicMock()
    repo = CeleryTaskLogRepository(db)
    db.execute.return_value = MagicMock(rowcount=0)

    claimed = repo.claim_for_redispatch("some-log-id")

    assert claimed is False
