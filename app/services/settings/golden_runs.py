"""
Golden-set runs for a scoring version, from the Settings page (settings phase 3, agreed 2026-11-12).

  - Estimate first: a cache-only pass (no LLM call) shows the LLM calls a run would make. A run can only start
    from an estimate of the version's exact configuration.
  - Daily cap: a run's calls are logged like any other (llm_call_log), so they count against the cap. It starts
    only when today's calls + the expected calls + a reserve for real candidates fit under the cap of the model it
    uses, and it stops before a case once they no longer would - so it never pushes real candidates into the
    no-LLM fallback ("awaiting full scoring"). It won't start, or stops, while any candidate is awaiting full
    scoring already. The reserve: the larger of GOLDEN_RUN_RESERVED_CALLS and the calls real scoring made in the
    previous quota day.
  - Promotion needs a PASSED run (full and no-LLM: no expectation failure, no counterfactual pair failing, no
    cache miss) whose config_hash is the version's configuration and model now.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, select

from app.exceptions.campaign_exceptions import CampaignException
from app.models.scoring_v2 import CandidateScoringResult, ScoringVersion, ScoringVersionGoldenRun

GOLDEN_CONTEXT = "golden_run"                  # scripts/golden/run.py LLM_SOURCE
RESERVED_CALLS_KEY = "GOLDEN_RUN_RESERVED_CALLS"
DEFAULT_RESERVED_CALLS = 100
CASE_ALLOWANCE = 30                            # the most calls one case can make (core skills x 3 votes, paraphrase)
ACTIVE = ("QUEUED", "RUNNING")
PURPOSE_PROMOTION, PURPOSE_REFERENCE = "PROMOTION", "REFERENCE_CHECK"
KIND_ESTIMATE, KIND_RUN = "ESTIMATE", "RUN"
STOP_RESERVE, STOP_AWAITING = "DAILY_LIMIT_RESERVE", "CANDIDATES_AWAITING_FULL_SCORING"
ESTIMATE_VALID_FOR = timedelta(hours=6)
# A run still QUEUED / RUNNING after this long was lost (its worker stopped): it no longer blocks new runs and
# is closed as ERROR (TIMED_OUT) when the next one starts. A real run takes minutes.
RUN_TIMEOUT = timedelta(hours=1)


def config_hash(version: ScoringVersion) -> str:
    from app.services.campaign.scoring_versions import version_config
    payload = json.dumps({"configuration": version_config(version), "model": version.model},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_view(run: ScoringVersionGoldenRun | None) -> dict | None:
    if run is None:
        return None
    return {"id": str(run.id), "scoring_version_id": str(run.scoring_version_id), "purpose": run.purpose,
            "kind": run.kind, "status": run.status, "config_hash": run.config_hash,
            "expected_llm_calls": run.expected_llm_calls, "llm_calls": run.llm_calls, "stop_reason": run.stop_reason,
            "summary": run.summary, "created_at": run.created_at.isoformat() if run.created_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}


def _models(db, version: ScoringVersion) -> list[str]:
    if version.model:
        return [version.model]
    from app.services.campaign.scoring_v2_service import provider_models
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    try:
        return list(provider_models(resolve_active_provider(db, LLMPurpose.EXTRACTION)))
    except Exception:
        return []


def reserved_calls(db, model: str | None) -> int:
    """Calls kept for real candidates: the setting, or more if real scoring needed more yesterday."""
    from app.models.llm_call_log import LLMCallLog
    from app.repositories.config_repository import ConfigRepository
    from app.services.llm.call_log import quota_day_start
    raw = ConfigRepository(db).get_configs_by_keys([RESERVED_CALLS_KEY]).get(RESERVED_CALLS_KEY)
    try:
        floor = max(int(float(raw)), 0) if raw is not None else DEFAULT_RESERVED_CALLS
    except (TypeError, ValueError):
        floor = DEFAULT_RESERVED_CALLS
    today = quota_day_start()
    yesterday = quota_day_start(today - timedelta(hours=1))
    query = select(func.count()).select_from(LLMCallLog).where(
        LLMCallLog.created_at >= yesterday, LLMCallLog.created_at < today, LLMCallLog.context != GOLDEN_CONTEXT)
    if model:
        query = query.where(LLMCallLog.model == model)
    return max(floor, int(db.execute(query).scalar() or 0))


def budget_problem(db, version: ScoringVersion, needed: int) -> str | None:
    """None when needed more calls fit with the reserve kept; otherwise why not (a stop code)."""
    if db.execute(select(func.count()).select_from(CandidateScoringResult).where(
            CandidateScoringResult.awaiting_full_scoring.is_(True),
            CandidateScoringResult.acted_on.is_(True))).scalar():
        return STOP_AWAITING
    from app.repositories.config_repository import ConfigRepository
    from app.services.campaign.scoring_v2_service import cap_for_model, daily_llm_call_caps
    from app.services.llm.call_log import calls_in_quota_day
    caps = daily_llm_call_caps(ConfigRepository(db))
    models = _models(db, version) or [None]
    for model in models:
        cap = cap_for_model(caps, model) if model else caps["default"]
        if calls_in_quota_day(db, model=model) + needed + reserved_calls(db, model) <= cap:
            return None
    return STOP_RESERVE


def budget_view(db, version: ScoringVersion, needed: int) -> dict:
    from app.repositories.config_repository import ConfigRepository
    from app.services.campaign.scoring_v2_service import cap_for_model, daily_llm_call_caps
    from app.services.llm.call_log import calls_in_quota_day
    caps = daily_llm_call_caps(ConfigRepository(db))
    model = (_models(db, version) or [None])[0]
    return {"model": model, "cap": cap_for_model(caps, model) if model else caps["default"],
            "used_today": calls_in_quota_day(db, model=model), "reserved_for_candidates": reserved_calls(db, model),
            "needed": needed}


STOP_MESSAGES = {
    STOP_RESERVE: "The run would use LLM calls real candidates may need today (the daily limit and its reserve).",
    STOP_AWAITING: "Candidates are waiting for full scoring: the daily LLM limit is already tight.",
}


class GoldenRunService:
    def __init__(self, db):
        self.db = db

    def _version(self, version_id) -> ScoringVersion:
        try:
            version = self.db.get(ScoringVersion, UUID(str(version_id)))
        except ValueError:
            version = None
        if version is None:
            raise CampaignException("Scoring version not found.", 404)
        return version

    def runs(self, version_id) -> dict:
        version = self._version(version_id)
        rows = (self.db.query(ScoringVersionGoldenRun)
                .filter(ScoringVersionGoldenRun.scoring_version_id == version.id)
                .order_by(ScoringVersionGoldenRun.created_at.desc()).limit(20).all())
        current_hash = config_hash(version)
        return {"config_hash": current_hash, "runs": [run_view(r) for r in rows],
                "promotion_run": run_view(self.passing_run(version))}

    def passing_run(self, version: ScoringVersion) -> ScoringVersionGoldenRun | None:
        """The latest PASSED run for exactly this configuration and model."""
        return (self.db.query(ScoringVersionGoldenRun)
                .filter(ScoringVersionGoldenRun.scoring_version_id == version.id,
                        ScoringVersionGoldenRun.kind == KIND_RUN, ScoringVersionGoldenRun.status == "PASSED",
                        ScoringVersionGoldenRun.config_hash == config_hash(version))
                .order_by(ScoringVersionGoldenRun.finished_at.desc()).first())

    def _purpose(self, version: ScoringVersion) -> str:
        if version.status == "candidate":
            return PURPOSE_PROMOTION
        if version.status == "current":
            return PURPOSE_REFERENCE
        raise CampaignException("A retired version can't be run.", 422)

    def _refuse_if_busy(self):
        now = datetime.now(timezone.utc)
        for run in self.db.query(ScoringVersionGoldenRun).filter(ScoringVersionGoldenRun.status.in_(ACTIVE)).all():
            if (run.started_at or run.created_at) < now - RUN_TIMEOUT:
                run.status, run.stop_reason, run.finished_at = "ERROR", "TIMED_OUT", now
                continue
            raise CampaignException("A golden run is already in progress. Wait for it to finish.", 409)
        self.db.flush()

    def start(self, version_id, kind: str, *, actor_id: str | None) -> dict:
        version = self._version(version_id)
        purpose = self._purpose(version)
        self._refuse_if_busy()
        expected = None
        digest = config_hash(version)
        if kind == KIND_RUN:
            estimate = (self.db.query(ScoringVersionGoldenRun)
                        .filter(ScoringVersionGoldenRun.scoring_version_id == version.id,
                                ScoringVersionGoldenRun.kind == KIND_ESTIMATE,
                                ScoringVersionGoldenRun.status == "ESTIMATED",
                                ScoringVersionGoldenRun.config_hash == digest)
                        .order_by(ScoringVersionGoldenRun.finished_at.desc()).first())
            if estimate is None or estimate.finished_at < datetime.now(timezone.utc) - ESTIMATE_VALID_FOR:
                raise CampaignException("Estimate the LLM calls first (an estimate is good for 6 hours).", 422,
                                        data={"code": "ESTIMATE_REQUIRED"})
            expected = estimate.expected_llm_calls or 0
            problem = budget_problem(self.db, version, expected + CASE_ALLOWANCE)
            if problem:
                raise CampaignException(STOP_MESSAGES[problem], 422,
                                        data={"code": problem, "budget": budget_view(self.db, version, expected)})
        elif kind != KIND_ESTIMATE:
            raise CampaignException("Unknown run kind.", 422)
        run = ScoringVersionGoldenRun(scoring_version_id=version.id, config_hash=digest, purpose=purpose, kind=kind,
                                      status="QUEUED", expected_llm_calls=expected, requested_by=actor_id)
        self.db.add(run)
        self.db.commit()
        from app.tasks.golden_run_tasks import golden_run_task
        golden_run_task.apply_async(kwargs={"run_id": str(run.id)})
        return run_view(run)


class EmailOrDispatchDuringGoldenRun(RuntimeError):
    """Raised if anything tries to queue an email or a task during an in-app golden run."""


@contextlib.contextmanager
def no_email_and_no_dispatch():
    """
    During an in-app golden run (approved 2026-11-12, besides the always-rolled-back transaction): no task can
    be dispatched (as in tests), no email notification can be created and the mail client can't send. Anything
    that tries raises, so the run ends in ERROR instead of reaching a candidate. The worker runs one task per
    process, so this only covers the run itself.
    """
    from unittest.mock import patch
    from celery.app.task import Task
    from app.repositories.email_notification_repository import EmailNotificationRepository

    def refuse(*_args, **_kwargs):
        raise EmailOrDispatchDuringGoldenRun("blocked during a golden run")
    with patch.object(Task, "apply_async", refuse), patch.object(Task, "delay", refuse),             patch.object(EmailNotificationRepository, "create", refuse),             patch.object(EmailNotificationRepository, "create_unless_duplicate", refuse),             patch("app.services.notifications.ses_email_client.SESEmailClient.send_email", refuse):
        # (by path, not imported: only the send task may use the mail client - tests/tasks/test_one_email_path)
        yield


def baseline_for_comparison(db, mode: str):
    """(accepted run dict, where it came from) for an in-app run: the current version's baseline in the database,
    or None (the runner then uses the repository's accepted run)."""
    from app.services.campaign.scoring_versions import current_version
    version = current_version(db)
    baseline = db.get(ScoringVersionGoldenRun, version.golden_baseline_run_id)         if version is not None and version.golden_baseline_run_id else None
    cases = ((baseline.results or {}).get(mode) or {}) if baseline is not None else {}
    if not cases:
        return None, "repository"
    return cases, f"baseline:{baseline.id}"


def execute(db, run_id: str, runner=None) -> None:
    """The worker's side: run the estimate or the run, and record its result. runner: scripts.golden.run.run."""
    run = db.get(ScoringVersionGoldenRun, UUID(run_id))
    if run is None or run.status != "QUEUED":
        return
    version = db.get(ScoringVersion, run.scoring_version_id)
    run.status, run.started_at = "RUNNING", datetime.now(timezone.utc)
    db.commit()
    if runner is None:
        from scripts.golden.run import run as runner
    try:
        if config_hash(version) != run.config_hash:
            run.status, run.stop_reason = "STOPPED", "CONFIGURATION_CHANGED"
        elif run.kind == KIND_ESTIMATE:
            holder: dict = {}
            accepted, source = baseline_for_comparison(db, "llm")
            with no_email_and_no_dispatch():
                runner(_args(version, holder, cache_only=True, accepted=accepted))
            holder.pop("_results", None)
            run.expected_llm_calls = int(holder.get("expected_llm_calls") or 0)
            run.summary = {"estimate": {**holder, "compared_with": source}}
            run.status = "ESTIMATED"
        else:
            def guard(case):
                return budget_problem(db, version, CASE_ALLOWANCE)
            full: dict = {}
            no_llm: dict = {}
            accepted, source = baseline_for_comparison(db, "llm")
            accepted_no_llm, source_no_llm = baseline_for_comparison(db, "no_llm")
            with no_email_and_no_dispatch():
                runner(_args(version, full, guard=guard, accepted=accepted))
                if not full.get("stopped"):
                    runner(_args(version, no_llm, no_llm=True, accepted=accepted_no_llm))
            run.results = {"llm": full.pop("_results", None), "no_llm": no_llm.pop("_results", None)}
            run.llm_calls = int(full.get("llm_calls") or 0) + int(no_llm.get("llm_calls") or 0)
            run.summary = {"llm": {**full, "compared_with": source},
                           "no_llm": {**no_llm, "compared_with": source_no_llm} if no_llm else no_llm}
            if full.get("stopped"):
                run.status, run.stop_reason = "STOPPED", full["stopped"]
            else:
                passed = all(not part.get("expectation_failures") and not part.get("pair_failures")
                             and not part.get("cache_misses") and part.get("cases_scored") == part.get("cases")
                             for part in (full, no_llm))
                run.status = "PASSED" if passed else "FAILED"
    except Exception as exc:
        db.rollback()
        run = db.get(ScoringVersionGoldenRun, UUID(run_id))
        run.status, run.stop_reason = "ERROR", type(exc).__name__[:40]
    run.finished_at = datetime.now(timezone.utc)
    db.commit()


def _args(version, holder, *, cache_only=False, no_llm=False, guard=None, accepted=None):
    return SimpleNamespace(only=None, cache_only=cache_only, no_llm=no_llm, dev_cache=False, accept=False,
                           accept_run=None, scoring_version_id=str(version.id), guard=guard,
                           result_holder=holder, no_report=True, accepted_override=accepted)
