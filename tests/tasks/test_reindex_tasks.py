from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import TaskStatus

TASKS_MODULE = "app.tasks.reindex_tasks"


class _Harness:
    def __init__(self):
        self.resume_repo = MagicMock()
        self.resume_repo.get_ivfflat_index_health.return_value = {
            "exists": True, "index_name": "idx_resume_embeddings_embedding", "size_bytes": 2048, "scan_count": 1,
        }
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
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_reindex_executes_reindex_index_statement_and_commits():
    from app.tasks.reindex_tasks import reindex_ivfflat_resume_embeddings

    with _Harness() as h:
        reindex_ivfflat_resume_embeddings.run()

        h.db.execute.assert_called_once()
        executed_sql = str(h.db.execute.call_args.args[0])
        assert "REINDEX INDEX idx_resume_embeddings_embedding" in executed_sql
        h.db.commit.assert_called_once()

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_reindex_marks_failure_and_rolls_back_on_error():
    from app.tasks.reindex_tasks import reindex_ivfflat_resume_embeddings

    with _Harness() as h:
        h.db.execute.side_effect = Exception("lock timeout")

        # Must not raise - a REINDEX failure is terminal bookkeeping, not
        # an unhandled Celery-level failure.
        reindex_ivfflat_resume_embeddings.run()

        h.db.rollback.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.FAILURE


def test_reindex_skips_duplicate_run_when_task_log_already_success():
    from app.tasks.reindex_tasks import reindex_ivfflat_resume_embeddings

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        reindex_ivfflat_resume_embeddings.run()

        h.db.execute.assert_not_called()
