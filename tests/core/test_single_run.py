"""
Review item 7: every scheduled task runs one at a time (app/core/single_run.py) - an overlapping run
skips instead of acting on the same rows twice (two sweeps both queueing the same emails).
"""
from contextlib import contextmanager

import pytest

from app.core import single_run as module


@pytest.fixture
def held_locks(monkeypatch):
    """A stand-in for the advisory lock: a name in this set is held by another run."""
    held: set[str] = set()

    @contextmanager
    def lock(name):
        if name in held:
            yield False
            return
        held.add(name)
        try:
            yield True
        finally:
            held.discard(name)

    monkeypatch.setattr(module, "_advisory_lock", lock)
    return held


def test_an_overlapping_run_does_nothing(held_locks):
    calls = []

    @module.single_run("scan.x")
    def scan(value):
        calls.append(value)
        return value

    assert scan(1) == 1
    held_locks.add("scan.x")
    assert scan(2) is None
    held_locks.discard("scan.x")
    assert scan(3) == 3
    assert calls == [1, 3]


def test_the_lock_is_released_when_the_run_raises(held_locks):
    @module.single_run("scan.y")
    def scan():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        scan()
    assert "scan.y" not in held_locks


def test_lock_ids_are_stable_and_distinct():
    assert module._lock_id("a.b") == module._lock_id("a.b")
    assert module._lock_id("a.b") != module._lock_id("a.c")
    assert 0 <= module._lock_id("a.b") < 2 ** 31


def test_every_scheduled_task_runs_one_at_a_time():
    from app.core.celery_app import celery_app
    celery_app.loader.import_default_modules()
    names = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert len(names) >= 13
    unguarded = [name for name in sorted(names)
                 if getattr(celery_app.tasks[name].run, "single_run_name", None) != name]
    assert not unguarded, f"scheduled tasks without @single_run: {unguarded}"
