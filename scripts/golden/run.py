"""
The golden runner: scores every golden-set case without saving anything, compares the results with the
cases' expectations, the counterfactual partners and the last accepted run, and writes a diff report.

    python -m scripts.golden.run                       score all cases (LLM only on vote-cache misses)
    python -m scripts.golden.run --only S1 CF-GAP-S7-1
    python -m scripts.golden.run --cache-only          no LLM call at all: a cache miss is a failure (CI)
    python -m scripts.golden.run --no-llm              score with the no-LLM fallback (checked against
                                                       expected_no_llm; its own accepted run)
    python -m scripts.golden.run --dev-cache           on a golden-cache miss, reuse dev's cached votes first
    python -m scripts.golden.run --accept              ... and accept this run (writes accepted_run.json
                                                       and the new vote-cache entries)
    python -m scripts.golden.run --accept-run tests/golden/runs/<stamp>.json
                                                       accept a run already reviewed, exactly as it was

How a case is scored: its JD, campaign, candidate and resume rows are built from the frozen files inside
one database transaction that is always rolled back; evidence is detected from the frozen text and parse;
scoring v2 runs with persist=False. The configuration (skill catalogue, rules, settings) is the connected
database's - dev's, or a fresh one built from migrations and the seed (point DB_* at it).

Votes come from tests/golden/vote_cache.json, keyed like the confirmation cache (resume text, core skills,
prompt, detection and model versions; per skill, the evidence the voters saw) but by natural keys - skills by
canonical name, not id (natural_keys.py) - so the same frozen votes answer on dev, a fresh database or
production. Only a miss calls the model.

Reports: tests/golden/runs/<timestamp>.md and .json (not committed). Exit code 1 when an expectation or a
counterfactual pair fails, or (with --cache-only) a vote was missing; changes from the accepted run alone
are reported, not failed - accepting them is the reviewed step.
"""
import argparse
import contextlib
import dataclasses
import datetime
import decimal
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.golden.fingerprint import scoring_fingerprint
from scripts.golden.golden_format import GOLDEN_DIR, counterfactual_pairs, load, load_json
from scripts.golden.natural_keys import SkillNames, natural_cache_keys

ACCEPTED = GOLDEN_DIR / "accepted_run.json"
# The no-LLM fallback's own accepted run (--no-llm): scan, auto-confirm and rules only.
ACCEPTED_NO_LLM = GOLDEN_DIR / "accepted_run_no_llm.json"


def accepted_path(no_llm: bool) -> Path:
    return ACCEPTED_NO_LLM if no_llm else ACCEPTED
VOTE_CACHE = GOLDEN_DIR / "vote_cache.json"
RUNS_DIR = GOLDEN_DIR / "runs"
LLM_SOURCE = "golden_run"


def norm(outcome: str | None) -> str | None:
    return {"REVIEW": "HOLD"}.get(outcome, outcome)


# --- building the rows --------------------------------------------------------------------------

def _row(model, values: dict, **extra):
    """A model row from frozen column values: enums, dates and numbers converted back by column type."""
    from sqlalchemy import DateTime, Enum as SAEnum
    columns = {c.key: c for c in model.__table__.columns}
    kwargs = {}
    for key, value in {**values, **extra}.items():
        column = columns.get(key)
        if column is None:
            continue
        if value is not None and isinstance(column.type, SAEnum) and column.type.enum_class is not None \
                and not isinstance(value, column.type.enum_class):
            value = column.type.enum_class(value)
        elif isinstance(value, str) and isinstance(column.type, DateTime):
            value = datetime.datetime.fromisoformat(value)
        elif isinstance(value, str) and _python_type(column) is decimal.Decimal:
            value = decimal.Decimal(value)
        kwargs[key] = value
    return model(**kwargs)


def _python_type(column):
    try:
        return column.type.python_type
    except NotImplementedError:
        return None


class World:
    """The rows golden cases are scored against, built inside the rolled-back transaction."""

    def __init__(self, db, golden: dict):
        from sqlalchemy import select
        from app.models.config import EncryptionKey
        from app.models.identity import User
        from app.models.prompt_template import PromptTemplate, PromptTemplateStatus
        from app.models.skills import SkillOntology
        self.db, self.golden = db, golden
        self.skill_ids = {name.lower(): sid for sid, name in db.execute(
            select(SkillOntology.id, SkillOntology.canonical_name).where(SkillOntology.is_active.is_(True))).all()}
        self.prompt_ids = {}
        for template in db.execute(select(PromptTemplate)).scalars():
            if template.name not in self.prompt_ids or template.status == PromptTemplateStatus.ACTIVE:
                self.prompt_ids[template.name] = template.id
        tag = uuid.uuid4().hex[:12]
        self.user = User(id=f"golden-{tag}", email=f"golden-{tag}@example.invalid", password_hash="-",
                         role="HR_ADMIN", full_name="Golden runner")
        db.add(self.user)
        self.key = db.execute(select(EncryptionKey)).scalars().first()
        if self.key is None:
            self.key = EncryptionKey(key_alias=f"golden-{tag}", purpose="golden runner")
            db.add(self.key)
        db.flush()
        self.jds: dict[str, tuple] = {}
        self.missing_skills: set[str] = set()

    def skill(self, name: str | None):
        if name is None:
            return None
        sid = self.skill_ids.get(name.lower())
        if sid is None:
            self.missing_skills.add(name)
        return sid

    def jd(self, key: str):
        if key in self.jds:
            return self.jds[key]
        from app.models.campaigns import HiringCampaign
        from app.models.jd.job_descriptions import JobDescription
        from app.models.skills import JDSkill, JDUnknownSkill
        from app.repositories.skill_repository import SkillRepository
        frozen = load_json(self.golden["jds"][key])
        row = frozen["job_description"]
        jd = _row(JobDescription, row, created_by=self.user.id,
                  prompt_template_id=self.prompt_ids[frozen["campaign"]["prompt_template"]],
                  content_hash=hashlib.sha256(f"golden:{key}:{uuid.uuid4()}".encode()).hexdigest())
        self.db.add(jd)
        self.db.flush()
        for skill in frozen["jd_skills"]:
            sid = self.skill(skill["skill"])
            if sid is None:
                continue
            self.db.add(_row(JDSkill, skill, jd_id=jd.id, canonical_skill_id=sid,
                             alias_skill_ids=[a for a in (self.skill(n) for n in skill.get("aliases") or []) if a] or None))
        repo = SkillRepository(self.db)
        for unknown in frozen.get("unknown_skills") or []:
            row_u, _ = repo.upsert_unknown_skill(unknown["raw_text"])
            self.db.add(_row(JDUnknownSkill, unknown, jd_id=jd.id, unknown_skill_id=row_u.id,
                             resolved_canonical_skill_id=self.skill(unknown.get("resolved_skill"))))
        settings = dict(frozen["campaign"])
        campaign = _row(HiringCampaign, settings, name=f"[GOLDEN] {key} {uuid.uuid4().hex[:6]}", jd_id=jd.id,
                        prompt_template_id=self.prompt_ids[settings["prompt_template"]],
                        ai_evaluate_prompt_id=self.prompt_ids.get(settings.get("ai_evaluate_prompt")),
                        hiring_manager_id=self.user.id, recruiter_id=self.user.id, created_by=self.user.id)
        self.db.add(campaign)
        self.db.flush()
        self.db.refresh(jd)
        self.jds[key] = (jd, campaign)
        return self.jds[key]

    def candidate(self, case: dict, frozen: dict, campaign):
        from app.models.candidates import Candidate, Resume
        from app.models.pipeline import CampaignCandidate
        from app.models.skills import CandidateSkill
        tag = uuid.uuid4().hex
        candidate = Candidate(full_name_encrypted=b"golden", email_encrypted=b"golden",
                              email_hash=hashlib.sha256(f"golden:{tag}".encode()).hexdigest(),
                              encryption_key_id=self.key.id,
                              jurisdiction=(frozen.get("candidate") or {}).get("jurisdiction") or "GLOBAL")
        self.db.add(candidate)
        self.db.flush()
        resume = _row(Resume, frozen["resume"], candidate_id=candidate.id, file_path=f"golden/{case['id']}",
                      file_hash=hashlib.sha256(f"golden:{tag}".encode()).hexdigest(), uploaded_by=self.user.id,
                      parsed_json=frozen["parsed_json"], original_filename=frozen.get("original_filename"))
        self.db.add(resume)
        self.db.flush()
        skills = []
        for skill in frozen["candidate_skills"]:
            row = _row(CandidateSkill, skill, candidate_id=candidate.id, resume_id=resume.id,
                       canonical_skill_id=self.skill(skill.get("skill")))
            self.db.add(row)
            skills.append(row)
        cc = CampaignCandidate(campaign_id=campaign.id, candidate_id=candidate.id, resume_id=resume.id,
                               idempotency_key=f"golden-{tag}")
        self.db.add(cc)
        self.db.flush()
        return cc, resume, skills


# --- the vote cache -----------------------------------------------------------------------------

class GoldenVotes:
    """
    The detection repository scoring v2 hands the confirmation service, with the confirmation cache served
    from tests/golden/vote_cache.json (and, with dev_cache, from dev's own cache on a golden miss).
    """

    def __init__(self, repo, cache: dict, *, dev_cache: bool, names=None, legacy: dict | None = None):
        self._repo, self.cache, self.dev_cache = repo, cache, dev_cache
        self.new: dict[str, dict] = {}
        self.hits = self.dev_hits = 0
        # Natural keys (natural_keys.py): names maps cached results to this database's skill ids; legacy maps
        # a natural key to its older id-based forms, under which entries cached before natural keys are stored -
        # such an entry is moved to its natural key (in new) and its old key retired when the run is accepted.
        self.names, self.legacy = names, legacy if legacy is not None else {}
        self.retired: list[str] = []
        # Two cases can share a confirmation cache key (same resume text and core skills) while
        # their evidence differs (e.g. S2 and its refrozen control) - one shared entry would hold
        # one case's answers and the other would miss on every alternate run. New answers are
        # kept per case ("<case>|<key>"), looked up before the plain key older entries use.
        self.case_id: str | None = None
        # The model scoring asks (the active assignment's main model); a cached vote answered by
        # any other model (a backup after a rate limit) is reported - and re-asked when wanted.
        self.main_model: str | None = None
        self.other_model_hits: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def _scoped(self, cache_key: str) -> str:
        return f"{self.case_id}|{cache_key}" if self.case_id else cache_key

    def _lookup(self, cache_key: str):
        """(entry, the key it was stored under): this case's own entry first, then the shared one."""
        scoped = self._scoped(cache_key)
        for key in (scoped, cache_key):
            entry = self.cache.get(key) or self.new.get(key)
            if entry is not None:
                return entry, key
        return None, None

    def get_confirmation(self, cache_key: str):
        scoped = self._scoped(cache_key)
        entry, _ = self._lookup(cache_key)
        for older in self.legacy.get(cache_key, []) if entry is None else []:
            entry, old_key = self._lookup(older)
            if entry is not None:                    # cached before natural keys: move it to its natural key
                self.new[scoped if old_key.startswith(f"{self.case_id}|") else cache_key] = entry
                self.retired.append(old_key)
                break
        if entry is not None and self.names is not None:
            entry = self.names.with_local_ids(entry)
        if entry is not None:
            self.hits += 1
            if self.main_model and entry.get("model") != self.main_model:
                self.other_model_hits.append({"case": self.case_id, "key": cache_key[:16],
                                              "model": entry.get("model"),
                                              "used_fallback": bool(entry.get("used_fallback"))})
            return SimpleNamespace(**entry)
        if self.dev_cache:
            row = self._repo.get_confirmation(cache_key)
            if row is not None:
                self.dev_hits += 1
                self.new[scoped] = {"results": row.results, "provider": row.provider, "model": row.model,
                                       "used_fallback": bool(row.used_fallback)}
                return row
        return None

    def record(self, outcome) -> None:
        """A confirmation outcome with votes asked now: kept for --accept (final answers only, as the cache does)."""
        if not outcome.cache_key or outcome.llm_calls == 0:
            return
        call = outcome.call
        self.new[self._scoped(outcome.cache_key)] = {
            "results": [r for r in outcome.results if r.get("evidence_hash")],
            "provider": getattr(call, "provider", None), "model": getattr(call, "model", None),
            "used_fallback": bool(getattr(call, "used_fallback", False)),
        }


# --- one case -----------------------------------------------------------------------------------

def score_case(world: World, case: dict, v2, config_repo, evidence_service, *, no_llm: bool = False) -> dict:
    from app.services.campaign import scoring_v2_service
    from app.services.skills.evidence_rules import roles_of
    from app.services.skills.skill_evidence_service import LLMSkillMatch, ParseInfo, job_titles_of, text_hash
    from app.services.campaign.validation_inputs import build_validation_inputs

    frozen = load_json(case["resume"])
    jd, campaign = world.jd(case["jd"])
    cc, resume, skills = world.candidate(case, frozen, campaign)
    parsed = frozen["parsed_json"] or {}
    with contextlib.redirect_stdout(io.StringIO()):
        experience, education, _weights, domain = build_validation_inputs(config_repo, jd, resume)
    parse = frozen.get("parse") or {}
    records = evidence_service.detect(
        resume.id, text=frozen["redacted_text"], persist=False,
        llm_matches=[LLMSkillMatch(s.canonical_skill_id, s.raw_extracted_text, s.match_tier)
                     for s in skills if s.canonical_skill_id],
        parse=ParseInfo(provider=parse.get("provider") or "FROZEN_PARSE", model=parse.get("model") or "frozen"),
        job_titles=job_titles_of(parsed), certifications=parsed.get("certifications"), roles=roles_of(parsed))
    # Stored evidence carries the resume text's hash, which keys the confirmation cache; evidence detected
    # here gets it too (the same hash dev's cache uses), so each resume has its own cache entry.
    digest = text_hash(frozen["redacted_text"])
    records = [SimpleNamespace(**{f.name: getattr(r, f.name) for f in dataclasses.fields(r)}, text_hash=digest)
               for r in records]
    with patch.object(scoring_v2_service, "redacted_resume_text", lambda r: frozen["redacted_text"]), \
            contextlib.redirect_stdout(io.StringIO()):          # the app prints its settings as it reads them
        breakdown = v2.score(campaign=campaign, campaign_candidate=cc, job_description=jd, resume=resume,
                             experience_result=experience, education_result=education, domain_result=domain,
                             acted_on=False, use_llm=True, persist=False, evidence_records=records,
                             use_confirmation_cache=True, llm_fallback="always" if no_llm else "never")
    return summarise(breakdown, records, experience, education, domain, ongoing=has_ongoing_role(parsed))


def has_ongoing_role(parsed: dict) -> bool:
    """A counted role still running ("present", is_current or no end date): its experience grows each month."""
    from app.services.resume.work_experience_duration import _CURRENT_MARKERS, is_career_break
    return any(isinstance(e, dict) and e.get("start_date") and not e.get("is_volunteer") and not is_career_break(e)
               and (e.get("is_current") or (e.get("end_date") or "").strip().lower() in _CURRENT_MARKERS | {""})
               for e in parsed.get("work_experience") or [])


def numeric_inputs(experience, education, domain) -> dict:
    """
    Every number the experience, education and domain checks hand to scoring, keyed "<check>.<field>". A years or
    score field that is None (a default applies, e.g. no JD tolerance) is kept, so a change to a set value shows.
    """
    out = {}
    for check, result in (("experience", experience), ("education", education), ("domain", domain)):
        for key, value in (result or {}).items():
            if key == "candidate_years" or isinstance(value, bool):  # candidate_years is experience_years
                continue
            if isinstance(value, (int, float, decimal.Decimal)):
                out[f"{check}.{key}"] = float(value) if isinstance(value, decimal.Decimal) else value
            elif value is None and ("years" in key or "score" in key):
                out[f"{check}.{key}"] = None
    return dict(sorted(out.items()))


def summarise(b: dict, records, experience, education=None, domain=None, *, ongoing: bool = False) -> dict:
    conf, para = b.get("confirmation") or {}, b.get("paraphrase") or {}
    para_by = {str(r["canonical_skill_id"]): r for r in para.get("results") or []}
    proposals = {}
    for p in para.get("proposals") or []:
        proposals.setdefault(str(p["canonical_skill_id"]), []).append(p["line"])
    evidence = {}
    for rec in records:
        evidence.setdefault(str(rec.canonical_skill_id), []).extend(
            f"[{(i.get('section') or '-')}] {i.get('text') or ''}"[:200] for i in rec.evidence)
    skills = {}
    for r in conf.get("results") or []:
        sid = str(r["canonical_skill_id"])
        p = para_by.get(sid)
        status = r.get("status")
        if p and p.get("status") == "CONFIRMED":
            status = "CONFIRMED"
        skills[r["name"]] = {
            "status": status, "method": "paraphrase" if p and p.get("status") == "CONFIRMED" else r.get("method"),
            "reason": r.get("reason"), "votes": r.get("votes") or [], "quote": (r.get("quote") or "")[:200],
            "paraphrase": None if not p else {"status": p.get("status"), "votes": p.get("votes") or [],
                                               "lines": proposals.get(sid, [])},
            "evidence": evidence.get(sid, [])[:5],
        }
    return {
        "outcome": norm(b.get("outcome")), "core": f"{b.get('core_confirmed_count')}/{b.get('core_skill_count')}",
        "score": b.get("score"), "failed_checks": b.get("failed_checks") or [],
        "needs_review_reason": b.get("needs_review_reason"),
        "experience_years": (experience or {}).get("candidate_years"),
        "experience_ongoing": ongoing,
        "scoring_mode": b.get("scoring_mode"),
        "awaiting_full_scoring": bool(b.get("awaiting_full_scoring")),
        "numeric_inputs": numeric_inputs(experience, education, domain),
        "skills": skills, "llm_calls": int(conf.get("llm_calls") or 0),
        "problem": conf.get("problem") or para.get("problem"),
        "scoring_config_version": b.get("scoring_config_version"),
    }


# --- comparing ----------------------------------------------------------------------------------

def check_expectations(case: dict, result: dict, *, no_llm: bool = False) -> list[str]:
    if no_llm:
        return check_no_llm_expectations(case, result)
    expected = case["expected"]
    problems = []
    if result["outcome"] not in expected["outcomes"]:
        problems.append(f"outcome {result['outcome']}, expected {'/'.join(expected['outcomes'])}")
    if expected.get("core") and result["core"] != expected["core"]:
        problems.append(f"core {result['core']}, expected {expected['core']}")
    if result["outcome"] == "PENDING":
        problems.append(f"PENDING: {result.get('problem')}")
    return problems


def check_no_llm_expectations(case: dict, result: dict) -> list[str]:
    """The fallback's outcome (expected_no_llm); a HOLD is always one awaiting full scoring - and never a vote."""
    expected = case["expected_no_llm"]
    problems = []
    if result["outcome"] not in expected["outcomes"]:
        problems.append(f"no-LLM outcome {result['outcome']}, expected {'/'.join(expected['outcomes'])} "
                        f"({expected.get('why')})")
    # Awaiting full scoring exactly when a core skill was left unconfirmed (an experience near-miss
    # with every core skill auto-confirmed is the full outcome already).
    unsettled = [n for n, s in result["skills"].items() if s["status"] != "CONFIRMED"]
    if bool(result.get("awaiting_full_scoring")) != (bool(unsettled) and result["outcome"] == "HOLD"):
        problems.append(f"awaiting full scoring is {bool(result.get('awaiting_full_scoring'))} with "
                        f"{len(unsettled)} core skill(s) unconfirmed and outcome {result['outcome']}")
    if result.get("scoring_mode") != "no_llm":
        problems.append(f"scored in mode {result.get('scoring_mode')}, not no_llm")
    if result.get("llm_calls"):
        problems.append(f"{result['llm_calls']} LLM call(s) in a no-LLM run")
    return problems


def skill_statuses(result: dict) -> dict:
    return {name: s["status"] for name, s in result["skills"].items()}


def compare_pair(case: dict, partner: dict, a: dict, b: dict) -> list[str]:
    """What differs between a counterfactual case (b) and the case it must match (a)."""
    out = []
    if a["outcome"] != b["outcome"]:
        out.append(f"outcome {b['outcome']} vs {a['outcome']} ({partner['id']})")
    sa, sb = skill_statuses(a), skill_statuses(b)
    for name in sorted(set(sa) | set(sb)):
        if sa.get(name) != sb.get(name):
            out.append(f"{name}: {sb.get(name)} vs {sa.get(name)} ({partner['id']}) - " + cause(a["skills"].get(name),
                                                                                               b["skills"].get(name)))
    return out


def cause(old: dict | None, new: dict | None) -> str:
    """Why a core skill's result differs: evidence, method, votes or paraphrase, in that order."""
    if old is None or new is None:
        return "skill added or removed from the JD's core skills"
    reasons = []
    gone = [e for e in old["evidence"] if e not in new["evidence"]]
    added = [e for e in new["evidence"] if e not in old["evidence"]]
    if gone or added:
        reasons.append("evidence " + "; ".join([f"- {e}" for e in gone[:2]] + [f"+ {e}" for e in added[:2]]))
    if old.get("method") != new.get("method"):
        reasons.append(f"method {old.get('method')} -> {new.get('method')}")
    if old.get("votes") != new.get("votes"):
        reasons.append(f"votes {old.get('votes')} -> {new.get('votes')}")
    op, np_ = old.get("paraphrase") or {}, new.get("paraphrase") or {}
    if op.get("lines") != np_.get("lines") or op.get("votes") != np_.get("votes"):
        reasons.append(f"paraphrase {op.get('lines')}/{op.get('votes')} -> {np_.get('lines')}/{np_.get('votes')}")
    if old.get("reason") != new.get("reason") and not reasons:
        reasons.append(f"reason {old.get('reason')} -> {new.get('reason')}")
    return "; ".join(reasons) or "same evidence and votes - see the full results"


CALENDAR = "calendar:"


def _months_since(run_at: str | None, now: datetime.datetime | None = None) -> int:
    if not run_at:
        return 0
    then, now = datetime.datetime.fromisoformat(run_at), now or datetime.datetime.now(datetime.timezone.utc)
    return (now.year * 12 + now.month) - (then.year * 12 + then.month)


def _numeric_changes(old: dict, result: dict, months: int) -> list[str]:
    """
    Experience years, the score and every numeric scoring input that differs from the accepted run. With an
    ongoing role, experience grows with the date: a change that matches the months since the accepted run is
    marked as calendar drift (it and the experience inputs that follow it), and still listed.
    """
    out = []
    a, b = old.get("experience_years"), result.get("experience_years")
    calendar = False
    if a != b:
        drift = (isinstance(a, (int, float)) and isinstance(b, (int, float)) and result.get("experience_ongoing")
                 and months > 0 and abs((b - a) - months / 12) <= 0.1 + 1e-9)
        calendar = bool(drift)
        out.append(f"experience years {a} -> {b}"
                   + (f" ({CALENDAR} an ongoing role, {months} month(s) since the accepted run)" if drift else ""))
    if old.get("score") != result.get("score"):
        out.append(f"score {old.get('score')} -> {result.get('score')}")
    oi, ni = old.get("numeric_inputs"), result.get("numeric_inputs") or {}
    if oi is None:                       # accepted before the inputs were recorded: nothing to compare with
        return out
    for key in sorted(set(oi) | set(ni)):
        if oi.get(key) != ni.get(key):
            follows = calendar and key.startswith("experience.")
            out.append(f"{key} {oi.get(key)} -> {ni.get(key)}" + (f" ({CALENDAR} follows the experience years)"
                                                                  if follows else ""))
    return out


def only_calendar(changes: list[str]) -> bool:
    return bool(changes) and all(CALENDAR in c for c in changes)


def compare_accepted(cid: str, result: dict, accepted: dict) -> list[str]:
    old = (accepted.get("cases") or {}).get(cid)
    if old is None:
        return ["new case (not in the accepted run)"]
    out = []
    if old["outcome"] != result["outcome"]:
        out.append(f"outcome {old['outcome']} -> {result['outcome']}")
    if old["core"] != result["core"]:
        out.append(f"core {old['core']} -> {result['core']}")
    out += _numeric_changes(old, result, _months_since(accepted.get("run_at")))
    so, sn = skill_statuses(old), skill_statuses(result)
    for name in sorted(set(so) | set(sn)):
        if so.get(name) != sn.get(name) or (old["skills"].get(name) or {}).get("method") != \
                (result["skills"].get(name) or {}).get("method"):
            out.append(f"{name}: {so.get(name)} ({(old['skills'].get(name) or {}).get('method')}) -> "
                       f"{sn.get(name)} ({(result['skills'].get(name) or {}).get('method')}) - "
                       + cause(old["skills"].get(name), result["skills"].get(name)))
    return out


# --- the run ------------------------------------------------------------------------------------

def run(args) -> int:
    import logging
    logging.disable(logging.WARNING)
    from app.core.config import settings
    if settings.email_delivery != "log":
        print("EMAIL_DELIVERY must be log - refusing to run.")
        return 2
    import app.models  # noqa: F401
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.core.redis_client import get_redis_client
    from app.db.session import engine
    from app.repositories.config_repository import ConfigRepository
    from app.repositories.skill_repository import SkillRepository
    from app.services.cache_service import CacheService
    from app.services.campaign.scoring_v2_service import ScoringV2Service
    from app.services.llm.base import LLMError
    from app.services.llm import call_log
    from app.services.llm.call_log import llm_call_context, llm_call_source
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    from app.services.skills import core_skill_confirmation_service as confirmation
    from app.services.campaign import scoring_v2_service

    golden = load()
    cases = [c for c in golden["cases"] if not args.only or c["id"] in args.only]
    if args.only and len(cases) != len(set(args.only)):
        print("unknown cases:", sorted(set(args.only) - {c["id"] for c in cases}))
        return 2
    accepted_file = accepted_path(args.no_llm)
    accepted = json.loads(accepted_file.read_text(encoding="utf-8")) if accepted_file.exists() else {}
    cache = json.loads(VOTE_CACHE.read_text(encoding="utf-8")) if VOTE_CACHE.exists() else {}
    started = datetime.datetime.now(datetime.timezone.utc)
    engine.echo = False
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    results, problems, misses = {}, {}, []
    try:
        config_repo = ConfigRepository(db)
        v2 = ScoringV2Service(db, SkillRepository(db), config_repo, cache_service=CacheService(get_redis_client()),
                              provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION))
        names, legacy_keys = SkillNames(db), {}
        votes = GoldenVotes(v2.detection_repository, cache, dev_cache=args.dev_cache, names=names, legacy=legacy_keys)
        votes.main_model = getattr(v2.provider_resolver(), "model", None)
        v2.detection_repository = votes
        world = World(db, golden)
        original_confirm = confirmation.CoreSkillConfirmationService.confirm

        def confirm(self, **kwargs):
            outcome = original_confirm(self, **kwargs)
            votes.record(outcome)
            return outcome

        def no_llm(*_args, **_kwargs):
            raise LLMError("golden vote cache miss (--cache-only)", reason="CACHE_MISS")

        patches = [patch.object(confirmation.CoreSkillConfirmationService, "confirm", confirm),
                   natural_cache_keys(confirmation, scoring_v2_service, names, legacy_keys)]
        if args.cache_only:
            patches.append(patch.object(confirmation, "structured_call", no_llm))
        # Every LLM call is logged in llm_call_log - but the case's resume and campaign rows live only
        # in this rolled-back transaction, so a call tagged with them can't be written (foreign key).
        # The golden run's calls are logged without them; a write that still fails is counted.
        log_write_failures = []
        original_recorder = call_log._recorder

        def recorder(row):
            try:
                original_recorder({**row, "resume_id": None, "campaign_id": None})
            except Exception as exc:
                log_write_failures.append(str(exc).splitlines()[0][:200])
                raise

        with contextlib.ExitStack() as stack:
            stack.callback(call_log.set_recorder, original_recorder)
            call_log.set_recorder(recorder)
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(llm_call_source(LLM_SOURCE))
            stack.enter_context(llm_call_context(LLM_SOURCE))       # apart from app usage in monitoring
            for case in cases:
                votes.case_id = case["id"]
                with db.begin_nested():
                    result = score_case(world, case, v2, config_repo, v2.evidence_service, no_llm=args.no_llm)
                results[case["id"]] = result
                if args.cache_only and result["outcome"] == "PENDING":
                    misses.append(case["id"])
                problems[case["id"]] = check_expectations(case, result, no_llm=args.no_llm)
                print(f"{case['id']:15} {result['outcome'] or '-':7} {result['core']:5} | calls {result['llm_calls']}"
                      + (f" | {'; '.join(problems[case['id']])}" if problems[case['id']] else ""), flush=True)
        if world.missing_skills:
            print("skills in the golden set missing from this database's catalogue:", sorted(world.missing_skills))
    finally:
        db.close()
        outer.rollback()
        connection.close()

    pairs = []
    for case, partner in counterfactual_pairs(golden):
        if case["id"] in results and partner["id"] in results:
            differences = compare_pair(case, partner, results[partner["id"]], results[case["id"]])
            pairs.append({"case": case["id"], "compare_with": partner["id"], "group": case["counterfactual"]["group"],
                          "varies": case["counterfactual"]["varies"], "detail": case["counterfactual"]["detail"],
                          "differences": differences})
    changes = {cid: compare_accepted(cid, r, accepted) for cid, r in results.items()} if accepted else {}
    with engine.connect() as c:
        calls, tokens_in, tokens_out = c.execute(text(
            "select count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) from llm_call_log "
            "where created_at >= :t and context = :ctx"), {"t": started, "ctx": LLM_SOURCE}).one()
    fingerprint = scoring_fingerprint()
    model = next((e.get("model") for e in votes.new.values() if e.get("model")), None)
    case_calls = sum(int(r.get("llm_calls") or 0) for r in results.values())
    new_other_model = {k: e.get("model") for k, e in votes.new.items()
                       if votes.main_model and e.get("model") != votes.main_model}
    run_record = {
        "run_at": started.isoformat(), "fingerprint": fingerprint, "cases": results,
        "mode": "no_llm" if args.no_llm else "llm",
        "llm": {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                # what the cases themselves counted - equal to `calls` unless logging failed
                "case_calls": case_calls, "log_write_failures": len(log_write_failures),
                "vote_cache_hits": votes.hits, "dev_cache_hits": votes.dev_hits, "new_cache_entries": len(votes.new),
                "model": model, "main_model": votes.main_model,
                "cached_votes_from_other_models": votes.other_model_hits,
                "new_votes_from_other_models": new_other_model},
        # id-keyed entries moved to their natural keys in this run (removed from the cache on accept)
        "retired_vote_cache_keys": sorted(set(votes.retired)),
    }
    failed_pairs = [p for p in pairs if p["differences"] and p["varies"] != "control"]
    failed = {cid: p for cid, p in problems.items() if p}
    report_path = write_report(run_record, failed, pairs, changes, accepted, misses, args, votes.new)
    print(f"\n{len(results)} cases | expectation failures {len(failed)} | counterfactual pairs failing "
          f"{len(failed_pairs)} of {len([p for p in pairs if p['varies'] != 'control'])} | changed since accepted "
          f"{len([c for c in changes.values() if c])} ({len([c for c in changes.values() if only_calendar(c)])} "
          f"calendar drift only) | LLM calls {calls} (tokens {tokens_in} in, {tokens_out} out) | "
          f"vote cache hits {votes.hits}, dev {votes.dev_hits}"
          + (f" | CALL LOG MISMATCH: cases made {case_calls}, {calls} logged, {len(log_write_failures)} write(s) failed"
             if case_calls != calls else "")
          + (f" | cached votes from a non-main model: {len(votes.other_model_hits)}" if votes.other_model_hits else "")
          + (f" | new votes from a non-main model: {len(new_other_model)}" if new_other_model else "")
          + f"\nreport: {report_path}")
    if args.accept:
        if args.only or misses:
            print("--accept needs the whole set and no cache misses - nothing accepted")
            return 2
        accept({**run_record, "new_vote_cache_entries": votes.new})
    if args.no_llm and calls:
        print(f"a --no-llm run made {calls} LLM call(s)")
        return 1
    return 1 if failed or failed_pairs or misses else 0


def accept(run_record: dict) -> None:
    """Accepted: the run's results and fingerprint (a --no-llm run in its own file), and its new vote-cache entries."""
    cache = json.loads(VOTE_CACHE.read_text(encoding="utf-8")) if VOTE_CACHE.exists() else {}
    new = run_record.get("new_vote_cache_entries") or {}
    for old_key in run_record.get("retired_vote_cache_keys") or []:   # moved to their natural keys
        cache.pop(old_key, None)
    cache.update(new)
    VOTE_CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    target = accepted_path(run_record.get("mode") == "no_llm")
    target.write_text(json.dumps({"accepted_on": datetime.date.today().isoformat(), "run_at": run_record["run_at"],
                                  "mode": run_record.get("mode", "llm"), "fingerprint": run_record["fingerprint"],
                                  "llm": run_record["llm"], "cases": run_record["cases"]},
                                 indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"accepted the run of {run_record['run_at'][:16]}: tests/golden/{target.name} and "
          f"{len(new)} new vote-cache entries")


def accept_saved_run(path: Path) -> int:
    """Accept a reviewed run exactly as it was - refused when scoring changed since it ran or it isn't complete."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    current = scoring_fingerprint()
    if record["fingerprint"] != current:
        print("Scoring changed since that run (fingerprint differs) - run the golden set again.")
        return 2
    missing = {c["id"] for c in load()["cases"]} - set(record["cases"])
    if missing:
        print("That run doesn't cover every case:", sorted(missing))
        return 2
    accept(record)
    return 0


def write_report(run_record, failed, pairs, changes, accepted, misses, args, new_entries) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = run_record["run_at"][:19].replace(":", "").replace("-", "")
    (RUNS_DIR / f"{stamp}.json").write_text(json.dumps({**run_record, "pairs": pairs, "changes": changes,
                                                         "new_vote_cache_entries": new_entries},
                                                        indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    llm = run_record["llm"]
    lines = [f"# Golden run {run_record['run_at'][:16]}", "",
             f"- Cases: {len(run_record['cases'])}" + (f" (only {', '.join(args.only)})" if args.only else ""),
             f"- LLM calls: {llm['calls']} (tokens {llm['input_tokens']} in, {llm['output_tokens']} out); "
             f"vote cache hits {llm['vote_cache_hits']}, from dev's cache {llm['dev_cache_hits']}, "
             f"new entries {llm['new_cache_entries']}",
             f"- Fingerprint: `{run_record['fingerprint'][:16]}`"
             + ("" if not accepted else f" (accepted run: `{accepted.get('fingerprint', '')[:16]}`, "
                                         f"{accepted.get('accepted_on')})"), ""]
    if llm.get("case_calls") is not None and llm["case_calls"] != llm["calls"]:
        lines += ["## LLM call log mismatch", "",
                  f"The cases made {llm['case_calls']} calls; {llm['calls']} are in llm_call_log "
                  f"({llm.get('log_write_failures', 0)} write(s) failed).", ""]
    if llm.get("cached_votes_from_other_models") or llm.get("new_votes_from_other_models"):
        lines += [f"## Votes not answered by the main model ({llm.get('main_model')})", ""]
        lines += [f"- {h['case']}: cached `{h['key']}` answered by {h['model']}"
                  + (" (backup after a failure)" if h["used_fallback"] else "")
                  for h in llm.get("cached_votes_from_other_models") or []]
        lines += [f"- new `{k[:24]}` answered by {m}" for k, m in (llm.get("new_votes_from_other_models") or {}).items()]
        lines.append("")
    if misses:
        lines += ["## Vote-cache misses (--cache-only)", "", *[f"- {m}" for m in misses], ""]
    lines += ["## Expectations", ""]
    lines += [f"- **{cid}**: {'; '.join(p)}" for cid, p in failed.items()] or ["All met."]
    lines += ["", "## Counterfactual pairs", "",
              "| Case | Compared with | Varies | Detail | Result |", "|---|---|---|---|---|"]
    for p in pairs:
        verdict = "same" if not p["differences"] else ("parse drift: " if p["varies"] == "control" else "**DIFFERS**: ") \
            + "; ".join(p["differences"])
        lines.append(f"| {p['case']} | {p['compare_with']} | {p['varies']} | {p['detail']} | {verdict} |")
    lines += ["", "## Changes since the accepted run", ""]
    if not accepted:
        lines.append("No accepted run yet.")
    else:
        changed = {cid: c for cid, c in changes.items() if c and not only_calendar(c)}
        drift = {cid: c for cid, c in changes.items() if only_calendar(c)}
        lines += [f"- **{cid}**: " + "; ".join(c) for cid, c in changed.items()] or ["None."]
        if drift:
            lines += ["", "Calendar drift only (experience on an ongoing role grows with the date):", ""]
            lines += [f"- {cid}: " + "; ".join(c) for cid, c in drift.items()]
    lines += ["", "## All results", "", "| Case | Outcome | Core | LLM calls | Failed checks |", "|---|---|---|---|---|"]
    for cid, r in run_record["cases"].items():
        lines.append(f"| {cid} | {r['outcome']} | {r['core']} | {r['llm_calls']} | {', '.join(r['failed_checks'])} |")
    path = RUNS_DIR / f"{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", metavar="CASE")
    parser.add_argument("--cache-only", action="store_true", help="never call the model; a cache miss fails")
    parser.add_argument("--no-llm", action="store_true",
                        help="score with the no-LLM fallback, checked against expected_no_llm")
    parser.add_argument("--dev-cache", action="store_true", help="on a golden-cache miss, reuse dev's cached votes")
    parser.add_argument("--accept", action="store_true", help="accept this run (whole set only)")
    parser.add_argument("--accept-run", metavar="RUN_JSON", help="accept a run already reviewed, as it was")
    args = parser.parse_args(argv)
    if args.accept_run:
        return accept_saved_run(Path(args.accept_run))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
