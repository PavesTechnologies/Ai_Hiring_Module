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
