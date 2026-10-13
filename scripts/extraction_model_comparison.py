"""
Extraction model comparison (Phase 2 Step 3) - read-only, writes nothing to the DB.

For each candidate in a campaign and each model: parse the resume (redacted
text, the campaign's prompt) N times with that model ALONE (saved key, no
backup, live config untouched), build the v2 evidence, run the core-skill
confirmation uncached with the same model, and score v2 on that run's own
evidence and parse. Reports per candidate and model: confirmed core count,
v2 score and outcome (PASS / HOLD / REJECT) per run, whether the outcome
changes between runs, latency, and tokens / cost from the provider's usage.

    python -m scripts.extraction_model_comparison --campaign "AI transformation lead Sep" \\
        --models gemini-3.5-flash-lite,gemini-3.8-flash --runs 3 --out comparison.json

Nothing is sent to candidates; scoring runs with persist=False and the
session is rolled back.
"""
import argparse
import json
import re
import sys
import time
from types import SimpleNamespace

from scripts.skill_detection_live_runs import _label, _redacted_text, _with_waits  # noqa: E402 (sets logging/echo)

from app.db.session import SessionLocal  # noqa: E402
from app.models.campaigns import HiringCampaign  # noqa: E402
from app.models.candidates import Resume  # noqa: E402
from app.models.pipeline import CampaignCandidate  # noqa: E402
from app.models.prompt_template import PromptTemplate  # noqa: E402
from app.repositories.config_repository import ConfigRepository  # noqa: E402
from app.repositories.skill_detection_repository import SkillDetectionRepository  # noqa: E402
from app.repositories.skill_repository import SkillRepository  # noqa: E402
from app.schemas.ai.resume_extraction_response import ResumeExtractionGenerationSchema  # noqa: E402
from app.services.ai.embedding_service import EmbeddingService  # noqa: E402
from app.services.campaign.scoring_v2_service import ScoringV2Service  # noqa: E402
from app.services.extractions.llm_extraction_service import LLMExtractionService  # noqa: E402
from app.services.llm.base import LLMError  # noqa: E402
from app.services.llm.factory import resolve_provider_model  # noqa: E402
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline  # noqa: E402
from app.services.skills.skill_evidence_service import (  # noqa: E402
    LLMSkillMatch, SkillEvidenceService, build_evidence, job_titles_of,
)
from app.services.skills.skill_normalization_service import SkillNormalizationService  # noqa: E402
from app.tasks.deterministic_scoring_tasks import build_validation_inputs  # noqa: E402

# USD per 1M tokens (standard paid tier; thinking tokens bill as output), as
# published on ai.google.dev/gemini-api/docs/pricing on 2026-10-13.
PRICES = {
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "from_2027": {"input": 1.50, "output": 7.50}},
    "gemini-3.6-flash": {"input": 1.50, "output": 7.50},
}
OUTCOME_LABEL = {"REVIEW": "HOLD"}       # v2's near-miss REVIEW lands candidates on HOLD
PENDING_RETRIES = 3


class UsageMeter:
    """Wraps a provider and adds up each call's token usage; everything else passes through."""

    def __init__(self, provider):
        self._provider = provider
        self.calls, self.input, self.output, self.thinking = 0, 0, 0, 0

    def generate_json(self, *args, **kwargs):
        result = self._provider.generate_json(*args, **kwargs)
        usage = getattr(self._provider, "last_usage", None) or {}
        self.calls += 1
        self.input += usage.get("input", 0)
        self.output += usage.get("output", 0)
        self.thinking += usage.get("thinking", 0)
        return result

    def snapshot(self) -> dict:
        return {"calls": self.calls, "input": self.input, "output": self.output, "thinking": self.thinking}

    def __getattr__(self, name):
        return getattr(self._provider, name)


def _delta(after: dict, before: dict) -> dict:
    return {k: after[k] - before[k] for k in after}


def _cost(model: str, usage: dict, prices: dict | None = None) -> float | None:
    price = prices or PRICES.get(model)
    if price is None:
        return None
    return (usage["input"] * price["input"] + (usage["output"] + usage["thinking"]) * price["output"]) / 1_000_000


def _parse(db, provider, skill_repo, text, template, resume_id, max_waits):
    raw = _with_waits(lambda: LLMExtractionService(provider).extract_raw(
        text, prompt=template, response_schema=ResumeExtractionGenerationSchema), max_waits, "parse")
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.skill_normalization_service = SkillNormalizationService(skill_repo, EmbeddingService(db))
    context = SimpleNamespace(raw_extraction=raw, redacted_text=text, resume_id=resume_id)
    pipeline._run_json_validation(context)
    pipeline._run_skill_normalization(context)
    matches = [LLMSkillMatch(m.canonical_skill_id, m.raw_text, m.match_tier.value)
               for m in context.skill_match_results if m.canonical_skill_id]
    return matches, context.validated_extraction


def _score(service, *, campaign, cc, jd, resume, parsed, records, config_repo):
    experience, education, _weights, domain = build_validation_inputs(config_repo, jd, SimpleNamespace(parsed_json=parsed))
    for attempt in range(PENDING_RETRIES + 1):
        breakdown = service.score(
            campaign=campaign, campaign_candidate=cc, job_description=jd, resume=resume,
            experience_result=experience, education_result=education, domain_result=domain,
            acted_on=False, persist=False, evidence_records=records, use_confirmation_cache=False,
        )
        if breakdown["outcome"] != "PENDING" or attempt == PENDING_RETRIES:
            return breakdown
        print("      (confirmation unavailable - waiting 30s)", flush=True)
        time.sleep(30)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, help="campaign name (case-insensitive, partial) or id")
    parser.add_argument("--models", required=True, help="comma-separated model ids, compared in this order")
    parser.add_argument("--provider", default="GOOGLE", help="provider whose saved key is used (default GOOGLE)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-waits", type=int, default=8)
    parser.add_argument("--out", required=True, help="JSON report path")
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    db = SessionLocal()
    report = {"models": models, "runs_per_resume": args.runs, "prices_per_1m": PRICES, "candidates": []}
    try:
        campaign = (db.query(HiringCampaign).filter(HiringCampaign.id == args.campaign).first()
                    if re.fullmatch(r"[0-9a-f-]{36}", args.campaign) else
                    db.query(HiringCampaign).filter(HiringCampaign.name.ilike(f"%{args.campaign}%")).first())
        if campaign is None:
            raise SystemExit(f"No campaign matching {args.campaign!r}")
        jd = campaign.job_description
        template = db.get(PromptTemplate, campaign.prompt_template_id).template_text
        skill_repo, config_repo = SkillRepository(db), ConfigRepository(db)
        tools = SkillEvidenceService(SkillDetectionRepository(db), skill_repo).load_tools()
        meters = {m: UsageMeter(resolve_provider_model(db, args.provider.upper(), m)) for m in models}
        report.update(campaign=campaign.name, jd=jd.title, provider=args.provider.upper())
        print(f"Campaign: {campaign.name}  |  JD: {jd.title}  |  models: {', '.join(models)}  |  runs: {args.runs}\n")

        rows = (db.query(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                .filter(CampaignCandidate.campaign_id == campaign.id).order_by(Resume.original_filename).all())
        for cc, resume in rows:
            text = _redacted_text(resume)
            label = _label(resume)
            candidate = {"candidate": label, "stage": cc.pipeline_stage.value, "models": {}}
            print(f"== {label}  (stage {cc.pipeline_stage.value})", flush=True)
            for model in models:
                meter = meters[model]
                service = ScoringV2Service(db, skill_repo, config_repo, provider_resolver=lambda m=meter: m)
                runs = []
                for run in range(args.runs):
                    entry = {"run": run + 1}
                    try:
                        # End the (read-only) transaction before each slow LLM call, so the
                        # connection isn't left idle in a transaction for the server to drop.
                        db.rollback()
                        before = meter.snapshot()
                        started = time.monotonic()
                        matches, parsed = _parse(db, meter, skill_repo, text, template, resume.id, args.max_waits)
                        entry["parse_seconds"] = round(time.monotonic() - started, 1)
                        entry["parse_usage"] = _delta(meter.snapshot(), before)
                        records = build_evidence(text=text, index=tools.index, resolver=tools.resolver,
                                                 llm_matches=matches, blocked=tools.blocked,
                                                 patterns=list(tools.patterns),
                                                 job_titles=job_titles_of(parsed))
                        db.rollback()
                        before = meter.snapshot()
                        started = time.monotonic()
                        breakdown = _score(service, campaign=campaign, cc=cc, jd=jd, resume=resume,
                                           parsed=parsed.model_dump(), records=records, config_repo=config_repo)
                        entry["confirm_seconds"] = round(time.monotonic() - started, 1)
                        entry["confirm_usage"] = _delta(meter.snapshot(), before)
                    except LLMError as exc:
                        entry["error"] = str(exc.reason)
                        runs.append(entry)
                        print(f"   {model} run {run + 1}: FAILED ({exc.reason})", flush=True)
                        continue
                    outcome = OUTCOME_LABEL.get(breakdown["outcome"], breakdown["outcome"])
                    entry.update(
                        outcome=outcome, v2_score=breakdown["deterministic_score"],
                        confirmed_core=breakdown["core_confirmed_count"], core_total=breakdown["core_skill_count"],
                        core_confirmed=breakdown["core_confirmed"], failed_checks=breakdown["failed_checks"],
                        skills_found=len(matches),
                        cost_usd=_cost(model, {k: entry["parse_usage"][k] + entry["confirm_usage"][k]
                                              for k in ("input", "output", "thinking")}),
                    )
                    runs.append(entry)
                    print(f"   {model:<24} run {run + 1}: core {entry['confirmed_core']}/{entry['core_total']}  "
                          f"v2 {entry['v2_score']:>6}  {outcome:<7}  skills {len(matches):>3}  "
                          f"parse {entry['parse_seconds']}s  confirm {entry['confirm_seconds']}s", flush=True)
                ok = [r for r in runs if "outcome" in r]
                candidate["models"][model] = {
                    "runs": runs,
                    "outcomes": [r["outcome"] for r in ok],
                    "outcome_changes_between_runs": len({r["outcome"] for r in ok}) > 1,
                    "failed_runs": len(runs) - len(ok),
                }
            report["candidates"].append(candidate)
            print(flush=True)

        report["usage_totals"] = {m: meters[m].snapshot() for m in models}
        report["cost_totals_usd"] = {m: _cost(m, meters[m].snapshot()) for m in models}
        if "from_2027" in PRICES.get("gemini-3.8-flash", {}) and "gemini-3.8-flash" in meters:
            report["cost_totals_usd"]["gemini-3.8-flash (2027 prices)"] = _cost(
                "gemini-3.8-flash", meters["gemini-3.8-flash"].snapshot(), PRICES["gemini-3.8-flash"]["from_2027"])
        print("Usage:", json.dumps(report["usage_totals"]))
        print("Cost (USD):", json.dumps(report["cost_totals_usd"]))
    finally:
        db.rollback()
        db.close()
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
