from unittest.mock import patch

import app.main as main_module

"""
Resume-upload resilience: the FastAPI startup hook recovers any
RESUME_DOCUMENT_PROCESSING upload whose Celery dispatch previously failed
(dispatch_failed=True), by calling the recovery logic directly - not via
a Celery dispatch, since the whole point is to still work even if
Celery/Redis are themselves unreachable at boot.
"""


def test_startup_hook_calls_recovery_and_does_not_raise():
    with patch("app.tasks.resume_processing_tasks.recover_stalled_resume_uploads", return_value=2) as mock_recover:
        main_module._recover_stalled_resume_uploads_on_startup()

    mock_recover.assert_called_once_with()


def test_startup_hook_swallows_recovery_failures():
    with patch(
        "app.tasks.resume_processing_tasks.recover_stalled_resume_uploads",
        side_effect=Exception("db unreachable at boot"),
    ):
        # Must not raise - a failed recovery scan must never block app startup.
        main_module._recover_stalled_resume_uploads_on_startup()
