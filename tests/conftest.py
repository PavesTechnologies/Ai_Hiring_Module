import os

# Standing rule: tests never send email. Forced before app settings load, so
# SESEmailClient only logs and never creates an SES client.
os.environ["EMAIL_DELIVERY"] = "log"

from unittest.mock import MagicMock

import pytest
from celery.app.task import Task


@pytest.fixture(autouse=True)
def _block_real_celery_dispatch(monkeypatch):
    """
    Never let a test put a real message on the broker. Tests run against the
    developer's live Redis, so an unmocked apply_async (e.g. an email task
    queued with a fake notification whose id is None) sits in the queue and
    is executed by the next real worker - it then fails against the shared
    database and lands in the dead-letter queue. Tests that care about a
    dispatch patch the task object itself, which this does not affect.
    """
    monkeypatch.setattr(Task, "apply_async", MagicMock(name="blocked_apply_async"))
    monkeypatch.setattr(Task, "delay", MagicMock(name="blocked_delay"))


@pytest.fixture(autouse=True)
def _email_is_log_only():
    """Fails loudly if anything switched the test run back to real SES delivery."""
    from app.core.config import settings
    settings.email_delivery = "log"
    yield
    assert settings.email_delivery == "log", "a test switched EMAIL_DELIVERY away from log-only"
