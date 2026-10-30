"""
Every LLM request counts: CountedProvider wraps each concrete provider the
factory builds (main and backup model, the Settings key check, scripts), and
records one llm_call_log row per real request - success or failure.

- source: the feature making the call, set with `llm_call_source("...")`
  around it (parsing, ai_evaluation, skill_confirmation, ...).
- context: "app" for the API and workers; a script sets LLM_CALL_CONTEXT
  (e.g. "dry_run") or `llm_call_context("...")`.
- resume_id / campaign_id: what the call was for, set with
  `llm_call_subject(resume_id=..., campaign_id=...)` around it (scoring,
  parsing, AI evaluation) - per-resume usage and cost in monitoring.
- Rows are written in their own short transaction, so a caller's rollback
  (dry runs) keeps them, and a failed write never fails the call.
- The usage day is the provider's quota day: Google resets its per-day
  quotas at midnight Pacific time.

Not visible here: retries inside the provider SDK on one request.
"""
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as day_time, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

QUOTA_DAY_ZONE = ZoneInfo("America/Los_Angeles")

_source: ContextVar[str | None] = ContextVar("llm_call_source", default=None)
_context: ContextVar[str | None] = ContextVar("llm_call_context", default=None)
_subject: ContextVar[dict | None] = ContextVar("llm_call_subject", default=None)


@contextmanager
def llm_call_source(label: str):
    """Labels the LLM calls made inside this block (the innermost label wins)."""
    token = _source.set(label)
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def llm_call_context(label: str):
    """Labels a whole run (a script, a dry run); else LLM_CALL_CONTEXT, else "app"."""
    token = _context.set(label)
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def llm_call_subject(*, resume_id=None, campaign_id=None):
    """Ties the LLM calls made inside this block to a resume and / or campaign (inner values win)."""
    outer = _subject.get() or {}
    token = _subject.set({"resume_id": resume_id or outer.get("resume_id"),
                          "campaign_id": campaign_id or outer.get("campaign_id")})
    try:
        yield
    finally:
        _subject.reset(token)


def current_subject() -> dict:
    return _subject.get() or {}


def current_source() -> str | None:
    return _source.get()


def current_context() -> str:
    return _context.get() or os.environ.get("LLM_CALL_CONTEXT") or "app"


def quota_day_start(now: datetime | None = None) -> datetime:
    """Start (UTC) of the provider's current quota day - midnight Pacific."""
    local = (now or datetime.now(timezone.utc)).astimezone(QUOTA_DAY_ZONE)
    return datetime.combine(local.date(), day_time(0), tzinfo=QUOTA_DAY_ZONE).astimezone(timezone.utc)


def _write(row: dict) -> None:
    from app.db.session import SessionLocal
    from app.models.llm_call_log import LLMCallLog
    db = SessionLocal()
    try:
        db.add(LLMCallLog(**row))
        db.commit()
    finally:
        db.close()


# Replaced in tests (tests/conftest.py) so no test writes to the database.
_recorder = _write


def set_recorder(recorder) -> None:
    global _recorder
    _recorder = recorder


def record_call(*, provider: str, model: str, purpose: str | None, outcome: str, latency_ms: int | None,
                usage: dict | None = None) -> None:
    row = {
        "provider": str(provider or "unknown")[:30], "model": str(model or "unknown")[:120],
        "purpose": str(purpose)[:30] if purpose else None,
        "source": (current_source() or None), "context": current_context()[:60],
        "outcome": str(outcome)[:40], "latency_ms": latency_ms,
        "resume_id": current_subject().get("resume_id"), "campaign_id": current_subject().get("campaign_id"),
        # Gemini's usage: input / output / thinking (thinking is billed as output).
        "input_tokens": (usage or {}).get("input"),
        "output_tokens": ((usage or {}).get("output") or 0) + ((usage or {}).get("thinking") or 0) if usage else None,
    }
    try:
        _recorder(row)
    except Exception:
        logger.warning("LLM call log write failed (%s/%s) - the call itself is unaffected.", provider, model,
                       exc_info=True)


class CountedProvider:
    """An LLM provider that records every request it makes; everything else passes through."""

    def __init__(self, inner, *, purpose: str | None = None):
        self._inner = inner
        self._purpose = purpose

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        # Transparent for writes too (e.g. a test swapping provider.client).
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    @property
    def inner(self):
        return self._inner

    def generate_json(self, prompt, response_schema, *, deterministic: bool = False) -> dict:
        started = time.monotonic()
        outcome = "ok"
        try:
            if deterministic:
                return self._inner.generate_json(prompt, response_schema, deterministic=True)
            # Plain call kept for providers/fakes without the keyword.
            return self._inner.generate_json(prompt, response_schema)
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            outcome = str(getattr(reason, "value", reason) or type(exc).__name__)
            raise
        finally:
            # A failed request leaves the previous call's usage in place: only a success reports tokens.
            usage = getattr(self._inner, "last_usage", None) if outcome == "ok" else None
            record_call(provider=getattr(self._inner, "provider_name", None), model=getattr(self._inner, "model", None),
                        purpose=self._purpose, outcome=outcome, latency_ms=int((time.monotonic() - started) * 1000),
                        usage=usage if isinstance(usage, dict) else None)

    def list_models(self):
        return self._inner.list_models()


def calls_in_quota_day(db, *, model: str | None = None, now: datetime | None = None) -> int:
    """LLM requests logged in the provider's current quota day (all, or one model's)."""
    from sqlalchemy import func, select
    from app.models.llm_call_log import LLMCallLog
    query = select(func.count()).select_from(LLMCallLog).where(LLMCallLog.created_at >= quota_day_start(now))
    if model:
        query = query.where(LLMCallLog.model == model)
    return int(db.execute(query).scalar() or 0)
