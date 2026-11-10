import os

# Standing rule: tests never send email. Forced before app settings load, so
# SESEmailClient only logs and never creates an SES client.
os.environ["EMAIL_DELIVERY"] = "log"

# P-M3: lookup hashes are keyed. Without any key material (CI without .env), tests get a fixed key; with
# a local .env they use the real derivation, so tests against the dev database match its stored hashes.
def _has_lookup_key_material() -> bool:
    from dotenv import dotenv_values
    names = ("LOOKUP_HASH_KEY", "ENCRYPTION_KEY_CANDIDATE_PII_V1")
    return any(os.environ.get(n) for n in names) or any(dotenv_values(".env").get(n) for n in names)


if not _has_lookup_key_material():
    os.environ["LOOKUP_HASH_KEY"] = "test-only-lookup-hash-key"

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
def _llm_calls_are_not_logged_to_the_database():
    """CountedProvider records each LLM request; in tests the rows go to a list, never to the live DB."""
    from app.services.llm import call_log
    rows: list[dict] = []
    original = call_log._recorder
    call_log.set_recorder(rows.append)
    yield rows
    call_log.set_recorder(original)


@pytest.fixture(autouse=True)
def _email_is_log_only():
    """Fails loudly if anything switched the test run back to real SES delivery."""
    from app.core.config import settings
    settings.email_delivery = "log"
    yield
    assert settings.email_delivery == "log", "a test switched EMAIL_DELIVERY away from log-only"


@pytest.fixture(autouse=True)
def _scheduled_tasks_take_no_real_lock(monkeypatch):
    """
    @single_run (app/core/single_run.py) takes a Postgres advisory lock around each scheduled task.
    Unit tests call those tasks directly with mocked sessions: the lock is always granted here and
    never touches the database. tests/core/test_single_run.py tests the lock itself.
    """
    from contextlib import contextmanager
    from app.core import single_run

    @contextmanager
    def granted(name):
        yield True

    monkeypatch.setattr(single_run, "_advisory_lock", granted)
