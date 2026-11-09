"""
One run at a time for a scheduled task (review item 7).

Beat can fire a scan while the previous one is still running (a slow tick, a redelivered message, two
beat processes during a deploy), and two overlapping sweeps would both act on the same rows - both
queue the same emails, both redispatch the same task. @single_run takes a Postgres advisory lock named
after the task for the length of the run; a run that can't take it does nothing and says so.

The lock is held on its own connection, so it is released when the run ends - or when the worker dies
and the connection drops. It is a session-level lock: it doesn't matter what the task's own sessions
commit or roll back.
"""
import functools
import logging
import zlib
from contextlib import contextmanager

from sqlalchemy import text

logger = logging.getLogger(__name__)

# A fixed first key keeps these locks apart from any other advisory lock in the database.
_LOCK_NAMESPACE = 0x41495253  # "AIRS"


def _lock_id(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF


@contextmanager
def _advisory_lock(name: str):
    """Yields True with the lock held, or False when another run holds it."""
    from app.db.session import engine
    with engine.connect() as connection:
        params = {"ns": _LOCK_NAMESPACE, "id": _lock_id(name)}
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:ns, :id)"), params).scalar()
        connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:ns, :id)"), params)
                connection.commit()


def single_run(name: str):
    """Decorator for a scheduled task's function: skip this run if one with the same name is running."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with _advisory_lock(name) as acquired:
                if not acquired:
                    logger.warning("Scheduled task %s skipped: the previous run is still going.", name)
                    return None
                return fn(*args, **kwargs)
        run.single_run_name = name
        return run
    return wrap
