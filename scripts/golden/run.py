"""
The golden runner: scores every golden-set case without saving anything, compares the results with the
cases' expectations, the counterfactual partners and the last accepted run, and writes a diff report.

    python -m scripts.golden.run                       score all cases (LLM only on vote-cache misses)
    python -m scripts.golden.run --only S1 CF-GAP-S7-1
    python -m scripts.golden.run --cache-only          no LLM call at all: a cache miss is a failure (CI)
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
prompt, detection and model versions; per skill, the evidence the voters saw). Only a miss calls the model.

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

ACCEPTED = GOLDEN_DIR / "accepted_run.json"
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

    def __init__(self, repo, cache: dict, *, dev_cache: bool):
        self._repo, self.cache, self.dev_cache = repo, cache, dev_cache
        self.new: dict[str, dict] = {}
        self.hits = self.dev_hits = 0

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def get_confirmation(self, cache_key: str):
        entry = self.cache.get(cache_key) or self.new.get(cache_key)
        if entry is not None:
            self.hits += 1
            return SimpleNamespace(**entry)
        if self.dev_cache:
            row = self._repo.get_confirmation(cache_key)
            if row is not None:
                self.dev_hits += 1
                self.new[cache_key] = {"results": row.results, "provider": row.provider, "model": row.model,
                                       "used_fallback": bool(row.used_fallback)}
                return row
        return None

    def record(self, outcome) -> None:
        """A confirmation outcome with votes asked now: kept for --accept (final answers only, as the cache does)."""
        if not outcome.cache_key or outcome.llm_calls == 0:
            return
        call = outcome.call
        self.new[outcome.cache_key] = {
            "results": [r for r in outcome.results if r.get("evidence_hash")],
            "provider": getattr(call, "provider", None), "model": getattr(call, "model", None),
            "used_fallback": bool(getattr(call, "used_fallback", False)),
        }


# --- one case -----------------------------------------------------------------------------------

def score_case(world: World, case: dict, v2, config_repo, evidence_service) -> dict:
    from app.services.campaign import scoring_v2_service
    from app.services.skills.evidence_rules import roles_of
    from app.services.skills.skill_evidence_service import LLMSkillMatch, ParseInfo, job_titles_of, text_hash
    from app.tasks.deterministic_scoring_tasks import build_validation_inputs

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
                             use_confirmation_cache=True)
    return summarise(breakdown, records, experience)


def summarise(b: dict, records, experience) -> dict:
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
        "skills": skills, "llm_calls": int(conf.get("llm_calls") or 0),
        "problem": conf.get("problem") or para.get("problem"),
        "scoring_config_version": b.get("scoring_config_version"),
    }


# --- comparing ----------------------------------------------------------------------------------

def check_expectations(case: dict, result: dict) -> list[str]:
    expected = case["expected"]
    problems = []
    if result["outcome"] not in expected["outcomes"]:
        problems.append(f"outcome {result['outcome']}, expected {'/'.join(expected['outcomes'])}")
    if expected.get("core") and result["core"] != expected["core"]:
        problems.append(f"core {result['core']}, expected {expected['core']}")
    if result["outcome"] == "PENDING":
        problems.append(f"PENDING: {result.get('problem')}")
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


def compare_accepted(cid: str, result: dict, accepted: dict) -> list[str]:
    old = (accepted.get("cases") or {}).get(cid)
    if old is None:
        return ["new case (not in the accepted run)"]
    out = []
    if old["outcome"] != result["outcome"]:
        out.append(f"outcome {old['outcome']} -> {result['outcome']}")
    if old["core"] != result["core"]:
        out.append(f"core {old['core']} -> {result['core']}")
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
    from app.services.llm.call_log import llm_call_source
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    from app.services.skills import core_skill_confirmation_service as confirmation

    golden = load()
    cases = [c for c in golden["cases"] if not args.only or c["id"] in args.only]
    if args.only and len(cases) != len(set(args.only)):
        print("unknown cases:", sorted(set(args.only) - {c["id"] for c in cases}))
        return 2
    accepted = json.loads(ACCEPTED.read_text(encoding="utf-8")) if ACCEPTED.exists() else {}
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
        votes = GoldenVotes(v2.detection_repository, cache, dev_cache=args.dev_cache)
        v2.detection_repository = votes
        world = World(db, golden)
        original_confirm = confirmation.CoreSkillConfirmationService.confirm

        def confirm(self, **kwargs):
            outcome = original_confirm(self, **kwargs)
            votes.record(outcome)
            return outcome

        def no_llm(*_args, **_kwargs):
            raise LLMError("golden vote cache miss (--cache-only)", reason="CACHE_MISS")

        patches = [patch.object(confirmation.CoreSkillConfirmationService, "confirm", confirm)]
        if args.cache_only:
            patches.append(patch.object(confirmation, "structured_call", no_llm))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(llm_call_source(LLM_SOURCE))
            for case in cases:
                with db.begin_nested():
                    result = score_case(world, case, v2, config_repo, v2.evidence_service)
                results[case["id"]] = result
                if args.cache_only and result["outcome"] == "PENDING":
                    misses.append(case["id"])
                problems[case["id"]] = check_expectations(case, result)
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
            "where created_at >= :t and source in ('golden_run', 'paraphrase_confirmation')"), {"t": started}).one()
    fingerprint = scoring_fingerprint()
    model = next((e.get("model") for e in votes.new.values() if e.get("model")), None)
    run_record = {
        "run_at": started.isoformat(), "fingerprint": fingerprint, "cases": results,
        "llm": {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "vote_cache_hits": votes.hits, "dev_cache_hits": votes.dev_hits, "new_cache_entries": len(votes.new),
                "model": model},
    }
    failed_pairs = [p for p in pairs if p["differences"] and p["varies"] != "control"]
    failed = {cid: p for cid, p in problems.items() if p}
    report_path = write_report(run_record, failed, pairs, changes, accepted, misses, args, votes.new)
    print(f"\n{len(results)} cases | expectation failures {len(failed)} | counterfactual pairs failing "
          f"{len(failed_pairs)} of {len([p for p in pairs if p['varies'] != 'control'])} | changed since accepted "
          f"{len([c for c in changes.values() if c])} | LLM calls {calls} (tokens {tokens_in} in, {tokens_out} out) | "
          f"vote cache hits {votes.hits}, dev {votes.dev_hits}\nreport: {report_path}")
    if args.accept:
        if args.only or misses:
            print("--accept needs the whole set and no cache misses - nothing accepted")
            return 2
        accept({**run_record, "new_vote_cache_entries": votes.new})
    return 1 if failed or failed_pairs or misses else 0


def accept(run_record: dict) -> None:
    """Accepted: the run's results and fingerprint, and its new vote-cache entries."""
    cache = json.loads(VOTE_CACHE.read_text(encoding="utf-8")) if VOTE_CACHE.exists() else {}
    new = run_record.get("new_vote_cache_entries") or {}
    cache.update(new)
    VOTE_CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    ACCEPTED.write_text(json.dumps({"accepted_on": datetime.date.today().isoformat(), "run_at": run_record["run_at"],
                                    "fingerprint": run_record["fingerprint"], "llm": run_record["llm"],
                                    "cases": run_record["cases"]}, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"accepted the run of {run_record['run_at'][:16]}: tests/golden/accepted_run.json and "
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
        changed = {cid: c for cid, c in changes.items() if c}
        lines += [f"- **{cid}**: " + "; ".join(c) for cid, c in changed.items()] or ["None."]
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
    parser.add_argument("--dev-cache", action="store_true", help="on a golden-cache miss, reuse dev's cached votes")
    parser.add_argument("--accept", action="store_true", help="accept this run (whole set only)")
    parser.add_argument("--accept-run", metavar="RUN_JSON", help="accept a run already reviewed, as it was")
    args = parser.parse_args(argv)
    if args.accept_run:
        return accept_saved_run(Path(args.accept_run))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
