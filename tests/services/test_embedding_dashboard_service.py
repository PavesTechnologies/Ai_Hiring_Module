from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.async_tasks import TaskStatus
from app.services.embedding_dashboard_service import EmbeddingDashboardService

TASKS_MODULE = "app.tasks.reindex_tasks"


def _make_model_version(name="all-MiniLM-L6-v2", version="v1"):
    return SimpleNamespace(model_name=name, model_version=version)


def _make_service(
    resume_count=100, jd_count=2, index_health=None, threshold_config=None, in_flight_count=0,
):
    resume_repo = MagicMock()
    resume_repo.count_embeddings.return_value = resume_count
    resume_repo.get_active_embedding_model_version.return_value = _make_model_version()
    resume_repo.get_ivfflat_index_health.return_value = index_health or {
        "exists": True, "index_name": "idx_resume_embeddings_embedding", "size_bytes": 1024, "scan_count": 0,
    }
    jd_repo = MagicMock()
    jd_repo.count_embeddings.return_value = jd_count
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = threshold_config or {}
    celery_task_log_repo = MagicMock()
    celery_task_log_repo.count_by_task_type_and_statuses.return_value = in_flight_count

    service = EmbeddingDashboardService(resume_repo, jd_repo, config_repo, celery_task_log_repo)
    return service, resume_repo, jd_repo, config_repo, celery_task_log_repo


def test_dashboard_reports_counts_and_estimated_storage():
    service, *_ = _make_service(resume_count=1000, jd_count=5)

    result = service.get_dashboard()

    assert result["resume_embeddings_count"] == 1000
    assert result["estimated_storage_bytes"] == 1000 * 384 * 4
    assert result["jd_embeddings_count"] == 5
    assert result["active_embedding_model_name"] == "all-MiniLM-L6-v2"
    assert result["active_embedding_model_version"] == "v1"


def test_dashboard_reports_index_health():
    service, *_ = _make_service(index_health={
        "exists": False, "index_name": "idx_resume_embeddings_embedding", "size_bytes": None, "scan_count": None,
    })

    result = service.get_dashboard()

    assert result["ivfflat_index_health"]["exists"] is False


def test_no_warning_or_reindex_when_below_threshold():
    service, *_, celery_task_log_repo = _make_service(
        resume_count=100, threshold_config={"EMBEDDING_REINDEX_THRESHOLD": "50000"},
    )

    with patch(f"{TASKS_MODULE}.reindex_ivfflat_resume_embeddings") as task_mock:
        result = service.get_dashboard()

        assert result["reindex_warning"] is False
        assert result["reindex_queued"] is False
        task_mock.apply_async.assert_not_called()


def test_warning_and_reindex_queued_when_above_threshold():
    service, *_, celery_task_log_repo = _make_service(
        resume_count=60000, threshold_config={"EMBEDDING_REINDEX_THRESHOLD": "50000"},
    )

    with patch(f"{TASKS_MODULE}.reindex_ivfflat_resume_embeddings") as task_mock:
        result = service.get_dashboard()

        assert result["reindex_warning"] is True
        assert result["reindex_queued"] is True
        task_mock.apply_async.assert_called_once()


def test_reindex_not_queued_twice_when_already_in_flight():
    service, *_, celery_task_log_repo = _make_service(
        resume_count=60000, threshold_config={"EMBEDDING_REINDEX_THRESHOLD": "50000"}, in_flight_count=1,
    )

    with patch(f"{TASKS_MODULE}.reindex_ivfflat_resume_embeddings") as task_mock:
        result = service.get_dashboard()

        assert result["reindex_warning"] is True
        assert result["reindex_queued"] is False
        task_mock.apply_async.assert_not_called()


def test_reindex_enqueue_failure_never_crashes_dashboard_read():
    service, *_, celery_task_log_repo = _make_service(
        resume_count=60000, threshold_config={"EMBEDDING_REINDEX_THRESHOLD": "50000"},
    )

    with patch(f"{TASKS_MODULE}.reindex_ivfflat_resume_embeddings") as task_mock:
        task_mock.apply_async.side_effect = Exception("broker unreachable")

        result = service.get_dashboard()

        assert result["reindex_queued"] is False


def test_falls_back_to_default_threshold_when_config_missing():
    service, *_ = _make_service(resume_count=100, threshold_config={})

    result = service.get_dashboard()

    assert result["reindex_threshold"] == 50000
