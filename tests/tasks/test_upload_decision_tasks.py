"""The daily expiry of pending bulk upload decisions (Celery beat), with the service mocked."""
from unittest.mock import MagicMock, patch

MODULE = "app.tasks.upload_decision_tasks"


def test_expiry_task_runs_the_service_and_logs_the_count():
    service = MagicMock()
    service.expire_pending.return_value = 2
    task_log_service = MagicMock()
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.CeleryTaskLogRepository"), \
            patch(f"{MODULE}.CeleryTaskLogService", return_value=task_log_service), \
            patch(f"{MODULE}.UploadDecisionService", return_value=service):
        from app.tasks.upload_decision_tasks import expire_upload_decisions_task
        expire_upload_decisions_task()
    service.expire_pending.assert_called_once_with()
    assert task_log_service.mark_success.call_args.kwargs["summary"] == '{"expired": 2}'


def test_expiry_failure_is_logged_not_raised():
    task_log_service = MagicMock()
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.CeleryTaskLogRepository"), \
            patch(f"{MODULE}.CeleryTaskLogService", return_value=task_log_service), \
            patch(f"{MODULE}.UploadDecisionService", side_effect=RuntimeError("db down")):
        from app.tasks.upload_decision_tasks import expire_upload_decisions_task
        expire_upload_decisions_task()
    task_log_service.mark_failure.assert_called_once()


def test_expiry_is_scheduled_daily():
    from app.core.celery_app import celery_app
    entry = celery_app.conf.beat_schedule["expire-upload-decisions"]
    assert entry["task"] == "resume.expire_upload_decisions"
    assert "app.tasks.upload_decision_tasks" in celery_app.conf.imports
    assert entry["schedule"].hour == {4} and entry["schedule"].minute == {30}
