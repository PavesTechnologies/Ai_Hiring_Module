"""
Worker diagnostics: the worker retries the broker forever (publishers keep
the 3-retry limit), logs to a rotating file when CELERY_WORKER_LOG_FILE is
set, and writes a start / stopping line so a restart's cause is visible.
"""
import logging
import logging.handlers
from types import SimpleNamespace
from unittest.mock import patch

import app.core.celery_app as celery_module
from app.core.celery_app import celery_app


def test_publishers_keep_the_3_retry_limit():
    assert celery_app.conf.broker_connection_max_retries == 3


def test_the_worker_retries_the_broker_forever():
    conf = SimpleNamespace(broker_connection_max_retries=3)
    celery_module._worker_keeps_retrying_the_broker(conf=conf)
    assert conf.broker_connection_max_retries is None


def _logger(name):
    logger = logging.getLogger(name)
    logger.handlers.clear()
    return logger


def test_a_rotating_log_file_is_added_once_when_configured(tmp_path):
    path = tmp_path / "logs" / "celery-worker.log"
    logger = _logger("test.worker.logfile")
    with patch.object(celery_module.settings, "celery_worker_log_file", str(path)), \
            patch.object(celery_module.settings, "celery_worker_log_max_bytes", 1234), \
            patch.object(celery_module.settings, "celery_worker_log_backup_count", 3):
        celery_module._worker_log_file(logger=logger, format="%(message)s")
        celery_module._worker_task_log_file(logger=logger, format="%(message)s")   # same logger: not twice
    handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 1234 and handlers[0].backupCount == 3
    logger.setLevel(logging.INFO)
    logger.info("hello from the worker")
    handlers[0].flush()
    assert "hello from the worker" in path.read_text(encoding="utf-8")
    handlers[0].close()


def test_no_log_file_without_the_setting():
    logger = _logger("test.worker.nolog")
    with patch.object(celery_module.settings, "celery_worker_log_file", None):
        celery_module._worker_log_file(logger=logger, format="%(message)s")
    assert logger.handlers == []


def test_start_and_stop_are_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="airs.worker"):
        celery_module._log_worker_started()
        celery_module._log_worker_stopping(sig="SIGTERM", how="Warm", exitcode=0)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("AIRS worker started") for m in messages)
    assert any("AIRS worker stopping" in m and "signal=SIGTERM" in m for m in messages)
