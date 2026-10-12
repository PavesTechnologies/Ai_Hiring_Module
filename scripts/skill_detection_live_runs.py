"""
Scoring v2 skill detection on a campaign's candidates - read-only, writes nothing to the DB.

Default (no LLM call, nothing leaves the system): re-extracts each resume's
text from storage, redacts PII locally, runs the v2 scan + union with the
STORED parse (candidate_skills), and compares the JD's core skills found by
v1 and by v2.

    python -m scripts.skill_detection_live_runs --campaign "AI transformation lead"

Live stability runs (calls the chosen provider's saved model, the same way
the pipeline does): parse each resume N times, optionally with the v2
prompt (RESUME_PARSE_V2), and confirm core skills; prints the core count per
run so runs and providers can be compared on the same input.

    python -m scripts.skill_detection_live_runs --campaign "..." --live --runs 3 --provider GROQ --confirm
    python -m scripts.skill_detection_live_runs --campaign "..." --live --provider GOOGLE --prompt v2 --out r.json

--provider defaults to the active provider; any provider with a verified
saved config can be named (GOOGLE / OPENAI / ANTHROPIC / GROQ). Rate limits
and outages are waited out (up to --max-waits per call), never skipped.
"""
import argparse
import json
import logging
import re
import sys
import time
from types import SimpleNamespace

logging.disable(logging.WARNING)

from app.db.session import SessionLocal, engine  # noqa: E402

engine.echo = False
import app.models  # noqa: E402,F401 - registers every mapper
from app.core.storage_service import StorageService  # noqa: E402
from app.models.campaigns import HiringCampaign  # noqa: E402
from app.models.candidates import Resume  # noqa: E402
from app.models.pipeline import CampaignCandidate  # noqa: E402
from app.models.prompt_template import PromptTemplate, PromptTemplateStatus  # noqa: E402
from app.models.skills import CandidateSkill, JDSkill, SkillOntology  # noqa: E402
from app.repositories.skill_detection_repository import SkillDetectionRepository  # noqa: E402
from app.repositories.skill_repository import SkillRepository  # noqa: E402
from app.schemas.ai.resume_extraction_response import ResumeExtractionGenerationSchema  # noqa: E402
from app.services.ai.embedding_service import EmbeddingService  # noqa: E402
from app.services.ai.preprocessing_service import PreprocessingService  # noqa: E402
from app.services.extractions.llm_extraction_service import LLMExtractionService  # noqa: E402
from app.services.llm.base import LLMError, LLMErrorReason  # noqa: E402
from app.services.llm.factory import resolve_active_provider, resolve_provider  # noqa: E402
from app.services.llm.model_selection import LLMPurpose  # noqa: E402
from app.services.llm.structured_call import call_info  # noqa: E402
from app.services.pii.pii_detection_service import PIIDetectionService  # noqa: E402
from app.services.pii.pii_redaction_service import PIIRedactionService  # noqa: E402
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline  # noqa: E402
from app.services.resume.resume_service import ResumeService  # noqa: E402
from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService  # noqa: E402
from app.services.skills.core_skill_confirmation_service import (  # noqa: E402
    CoreSkillConfirmationService, definite_sense_mismatch, jd_core_skill_specs,
)
from app.services.skills.skill_evidence_service import (  # noqa: E402
    LLMSkillMatch, SkillEvidenceService, build_evidence, job_titles_of, text_hash,
)
from app.services.skills.skill_normalization_service import SkillNormalizationService  # noqa: E402
from app.tasks.resume_processing_tasks import _FILE_FORMAT_TO_SOURCE_FORMAT  # noqa: E402

V2_PROMPT_TASK_TYPE = "RESUME_PARSE_V2"
_WAIT_REASONS = {LLMErrorReason.RATE_LIMITED, LLMErrorReason.UNAVAILABLE}


def _with_waits(fn, max_waits: int, label: str):
    """Runs fn, waiting out rate limits / outages (the provider's retry-after, else 30s) up to max_waits times."""
    for attempt in range(max_waits + 1):
        try:
            return fn()
        except LLMError as exc:
            if exc.reason not in _WAIT_REASONS or attempt == max_waits:
                raise
            wait = min(max(int(exc.retry_after_seconds or 30), 5), 120)
            print(f"      ({label}: {exc.reason}, waiting {wait}s)", flush=True)
            time.sleep(wait)


def _redacted_text(resume) -> str:
    content = StorageService().download_file(bucket_name=ResumeService.RESUME_STORAGE_BUCKET, file_path=resume.file_path)
    text = PreprocessingService().normalize(
        ResumeTextExtractionService.extract(content, _FILE_FORMAT_TO_SOURCE_FORMAT[resume.file_format]))
    return PIIRedactionService().redact(text, PIIDetectionService().detect(text))


def _label(resume) -> str:
    stem = re.sub(r"\.[a-z]+$", "", resume.original_filename or str(resume.id), flags=re.I)
    return re.sub(r"[_\[\]()]+", " ", stem.split("_", 1)[-1]).strip()[:28]


def _v1_core(cc) -> set[str]:
    breakdown = cc.deterministic_breakdown or {}
    return {s.get("canonical_name") for s in breakdown.get("mandatory_skills", [])
            if s.get("importance") == "core" and s.get("match_type") != "MISSING"}


def _core_found(specs, records) -> list[str]:
    """Core skills with evidence that isn't a definite sense mismatch (an upper bound before confirmation)."""
    return [spec.name for spec in specs
            if any(r.canonical_skill_id in spec.skill_ids and not definite_sense_mismatch(spec, r.sense)
                   for r in records)]


def _describe(specs, records) -> list[str]:
    lines = []
    for spec in specs:
        rows = [r for r in records if r.canonical_skill_id in spec.skill_ids]
        if not rows:
            lines.append(f"      - {spec.name}: not found")
            continue
        for r in rows:
            sense = f" sense={r.sense or 'unresolved'}" if spec.sense else ""
            mismatch = " (DEFINITE SENSE MISMATCH - not core)" if definite_sense_mismatch(spec, r.sense) else ""
            lines.append(f"      - {spec.name}: {'/'.join(r.sources)} rec={float(r.recognition):.1f}{sense}{mismatch}"
                         f"  \"{(r.evidence[0]['text'] if r.evidence else '')[:70]}\"")
    return lines


def _parse(db, provider, skill_repo, text, template, resume_id, max_waits):
    raw = _with_waits(lambda: LLMExtractionService(provider).extract_raw(
        text, prompt=template, response_schema=ResumeExtractionGenerationSchema), max_waits, "parse")
    parsed_by = call_info(provider)
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.skill_normalization_service = SkillNormalizationService(skill_repo, EmbeddingService(db))
    context = SimpleNamespace(raw_extraction=raw, redacted_text=text, resume_id=resume_id)
    pipeline._run_json_validation(context)
    pipeline._run_skill_normalization(context)
    matches = [LLMSkillMatch(m.canonical_skill_id, m.raw_text, m.match_tier.value)
               for m in context.skill_match_results if m.canonical_skill_id]
    return matches, parsed_by, job_titles_of(context.validated_extraction)


def main() -> None:
    # Resume text has characters the Windows console code page can't print.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, help="campaign name (case-insensitive, partial) or id")
    parser.add_argument("--live", action="store_true", help="re-parse with the LLM instead of using the stored parse")
    parser.add_argument("--runs", type=int, default=3, help="live runs per resume (default 3)")
    parser.add_argument("--provider", help="GOOGLE / OPENAI / ANTHROPIC / GROQ (default: the active provider)")
    parser.add_argument("--prompt", choices=["campaign", "v2"], default="campaign", help="resume prompt for --live")
    parser.add_argument("--confirm", action="store_true", help="also run the LLM core-skill confirmation")
    parser.add_argument("--max-waits", type=int, default=8, help="rate-limit/outage waits per call (default 8)")
    parser.add_argument("--out", help="also write the per-run results to this JSON file")
    args = parser.parse_args()

    db = SessionLocal()
    report = {"runs": []}
    try:
        campaign = (db.query(HiringCampaign).filter(HiringCampaign.id == args.campaign).first()
                    if re.fullmatch(r"[0-9a-f-]{36}", args.campaign) else
                    db.query(HiringCampaign).filter(HiringCampaign.name.ilike(f"%{args.campaign}%")).first())
        if campaign is None:
            raise SystemExit(f"No campaign matching {args.campaign!r}")
        jd = campaign.job_description
        skill_repo = SkillRepository(db)
        tools = SkillEvidenceService(SkillDetectionRepository(db), skill_repo).load_tools()
        jd_skills = db.query(JDSkill).filter(JDSkill.jd_id == jd.id).all()
        names = dict(db.query(SkillOntology.id, SkillOntology.canonical_name)
                     .filter(SkillOntology.id.in_([s.canonical_skill_id for s in jd_skills])).all())
        specs = jd_core_skill_specs(jd, jd_skills, names, tools.resolver)

        provider = None
        if args.live or args.confirm:
            provider = (resolve_provider(db, args.provider.upper(), LLMPurpose.EXTRACTION) if args.provider
                        else resolve_active_provider(db, LLMPurpose.EXTRACTION))
        template = None
        if args.live:
            if args.prompt == "v2":
                row = (db.query(PromptTemplate).filter(PromptTemplate.task_type == V2_PROMPT_TASK_TYPE,
                                                       PromptTemplate.status == PromptTemplateStatus.ACTIVE)
                       .order_by(PromptTemplate.created_at.desc()).first())
                if row is None:
                    raise SystemExit(f"No ACTIVE {V2_PROMPT_TASK_TYPE} prompt template")
            else:
                row = db.get(PromptTemplate, campaign.prompt_template_id)
            template = row.template_text

        header = {
            "campaign": campaign.name, "jd": jd.title,
            "core_skills": [{"name": s.name, "sense": s.sense, "sense_basis": s.sense_basis} for s in specs],
            "provider": getattr(provider, "provider_name", None), "model": getattr(provider, "model", None),
            "prompt": args.prompt if args.live else "stored parse", "runs_per_resume": args.runs if args.live else 1,
        }
        report.update(header)
        print(f"Campaign: {campaign.name}  |  JD: {jd.title}  |  scoring_v2_enabled={campaign.scoring_v2_enabled}")
        print("Core skills: " + ", ".join(
            f"{s.name}" + (f" [{s.sense} - {s.sense_basis}]" if s.sense else "") for s in specs))
        if provider is not None:
            print(f"Provider: {provider.provider_name} / {provider.model}" + (f"  prompt={args.prompt}" if args.live else ""))
        print(flush=True)

        rows = (db.query(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                .filter(CampaignCandidate.campaign_id == campaign.id).order_by(Resume.original_filename).all())
        for cc, resume in rows:
            text = _redacted_text(resume)
            label = _label(resume)
            v1 = _v1_core(cc)
            print(f"== {label}  (stage {cc.pipeline_stage.value}; v1 core matched {len(v1)}/{len(specs)})", flush=True)
            runs = []
            for run in range(args.runs if args.live else 1):
                entry = {"candidate": label, "run": run + 1, "v1_core": sorted(v1)}
                try:
                    if args.live:
                        matches, parsed_by, titles = _parse(db, provider, skill_repo, text, template, resume.id,
                                                            args.max_waits)
                    else:
                        parsed_by = None
                        titles = job_titles_of(resume.parsed_json)
                        matches = [LLMSkillMatch(s.canonical_skill_id, s.raw_extracted_text, s.match_tier)
                                   for s in db.query(CandidateSkill).filter(CandidateSkill.resume_id == resume.id,
                                                                             CandidateSkill.canonical_skill_id.isnot(None))]
                except LLMError as exc:
                    print(f"   run {run + 1}: PARSE FAILED ({exc.reason})", flush=True)
                    entry["error"] = f"parse: {exc.reason}"
                    report["runs"].append(entry)
                    runs.append(("failed",))
                    continue
                records = build_evidence(text=text, index=tools.index, resolver=tools.resolver,
                                         llm_matches=matches, blocked=tools.blocked, patterns=list(tools.patterns),
                                         job_titles=titles)
                found = _core_found(specs, records)
                entry.update(found=found, parsed_by=parsed_by.as_dict() if parsed_by else None)
                line = f"   run {run + 1}: v2 core found {len(found)}/{len(specs)}"
                if parsed_by:
                    line += f"  (parsed by {parsed_by.model}{' BACKUP' if parsed_by.used_fallback else ''})"
                statuses = None
                if args.confirm:
                    outcome = _with_waits(lambda: CoreSkillConfirmationService(provider).confirm(
                        resume_id=resume.id, jd_id=jd.id, core_skills=specs,
                        evidence_rows=records, text_hash=text_hash(text), use_cache=False, persist=False,
                    ), args.max_waits, "confirm")
                    statuses = {r["name"]: r.get("status") for r in outcome.results}
                    confirmed = [n for n, st in statuses.items() if st == "CONFIRMED"]
                    entry.update(votes={r["name"]: r.get("votes") for r in outcome.results},
                                 methods={r["name"]: r.get("method") for r in outcome.results},
                                 llm_calls=outcome.llm_calls)
                    entry.update(statuses=statuses, confirm_problem=outcome.problem,
                                 confirmed_by=outcome.call.as_dict() if outcome.call else None,
                                 quotes={r["name"]: r.get("quote") for r in outcome.results if r.get("quote")})
                    line += f"  confirmed {len(confirmed)}/{len(specs)}"
                    if outcome.call:
                        line += f" (by {outcome.call.model}{' BACKUP' if outcome.call.used_fallback else ''})"
                    if outcome.problem:
                        line += f"  [{outcome.problem[:60]}]"
                runs.append((tuple(found), tuple(sorted((statuses or {}).items()))))
                report["runs"].append(entry)
                print(line, flush=True)
                if run == 0:
                    print("\n".join(_describe(specs, records)))
                if statuses is not None:
                    print("      " + "; ".join(f"{n}: {st}" for n, st in statuses.items()), flush=True)
            if len(runs) > 1:
                ok = [r for r in runs if r != ("failed",)]
                if len(ok) < 2:
                    print(f"   stable: n/a ({len(runs) - len(ok)} of {len(runs)} runs failed)")
                else:
                    stable = len(set(ok)) == 1
                    failed_note = f", {len(runs) - len(ok)} failed" if len(ok) < len(runs) else ""
                    print(f"   stable across {len(ok)} runs{failed_note}: {'YES' if stable else 'NO'}")
            print(flush=True)
    finally:
        db.rollback()
        db.close()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
