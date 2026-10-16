"""
Phase 3 Step 6 dry run: score v2 for every campaign candidate with the
paraphrase round and save NOTHING (persist=False; the session is rolled
back). Reports per candidate: the stored v2 outcome, the dry-run outcome,
the paraphrase targets, each target's best-matching experience lines with
their similarity (and why any were excluded), the proposals, the votes, and
the LLM calls.

Needs migration b7d9f1a3c5e7 (skill_ontology.description) applied: the run
never changes the schema, and stops if the column is missing.

The first confirmation round reuses cached answers like production; the
paraphrase round has its own cache key, so it asks the model (persist=False:
nothing is cached, nothing counts against the daily cap).

Usage: python scripts/paraphrase_eval.py <out.json> [--threshold 0.45]
"""
import argparse
import json
import logging
import time
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402

engine.echo = False
import app.models  # noqa: E402,F401
from app.models.campaigns import HiringCampaign  # noqa: E402
from app.models.candidates import Resume  # noqa: E402
from app.models.pipeline import CampaignCandidate  # noqa: E402
from app.repositories.config_repository import ConfigRepository  # noqa: E402
from app.repositories.skill_repository import SkillRepository  # noqa: E402
from app.services.campaign.scoring_v2_service import ScoringV2Service  # noqa: E402
from app.services.llm.factory import resolve_active_provider  # noqa: E402
from app.services.llm.model_selection import LLMPurpose  # noqa: E402
from app.tasks.deterministic_scoring_tasks import build_validation_inputs  # noqa: E402

def _require_descriptions(db) -> str:
    exists = db.execute(text("select 1 from information_schema.columns where table_name = 'skill_ontology' "
                             "and column_name = 'description'")).first()
    if not exists:
        raise SystemExit("skill_ontology.description is missing - apply migration b7d9f1a3c5e7 first.")
    return "column present"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds to wait between candidates (stay under the provider's per-minute limit)")
    args = parser.parse_args()

    db = SessionLocal()
    report = {"candidates": []}
    try:
        report["descriptions"] = _require_descriptions(db)
        config_repo = ConfigRepository(db)
        service = ScoringV2Service(db, SkillRepository(db), config_repo,
                                   provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION))
        if args.threshold is not None:
            base = service.load_config
            service.load_config = lambda: {**base(), "paraphrase": {**base()["paraphrase"], "threshold": args.threshold}}
        report["paraphrase_config"] = service.load_config()["paraphrase"]
        rows = (db.query(CampaignCandidate, Resume, HiringCampaign)
                .join(Resume, Resume.id == CampaignCandidate.resume_id)
                .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
                .order_by(HiringCampaign.name, Resume.original_filename).all())
        for n, (cc, resume, campaign) in enumerate(rows):
            if n and args.pause:
                time.sleep(args.pause)
            jd = campaign.job_description
            stored = db.execute(text("select outcome from candidate_scoring_results where campaign_candidate_id = :c "
                                     "and scoring_version = 'v2' order by created_at desc limit 1"),
                                {"c": cc.id}).scalar()
            experience, education, _, domain = build_validation_inputs(config_repo, jd, resume)
            b = service.score(campaign=campaign, campaign_candidate=cc, job_description=jd, resume=resume,
                              experience_result=experience, education_result=education, domain_result=domain,
                              acted_on=False, use_llm=True, persist=False)
            para = b.get("paraphrase") or {}
            entry = {
                "campaign": campaign.name, "file": resume.original_filename, "stored_v2": stored,
                "dry_v2": b["outcome"], "core": f"{b['core_confirmed_count']}/{b['core_skill_count']}",
                "core_status": {e["canonical_name"]: e.get("confirmation_status")
                                for e in b["mandatory_skills"] if e.get("importance") == "core"},
                "failed_checks": b.get("failed_checks"),
                "llm_calls_total": (b.get("confirmation") or {}).get("llm_calls"),
                "llm_calls_paraphrase": para.get("llm_calls", 0),
                "targets": para.get("targets", []),
                "top_similarities": para.get("top_similarities", {}),
                "proposals": para.get("proposals", []),
                "votes": [{"skill": r.get("name"), "status": r.get("status"), "votes": r.get("votes"),
                           "quotes": r.get("vote_quotes")} for r in para.get("results") or []],
                "problem": para.get("problem"),
            }
            report["candidates"].append(entry)
            print(f"{campaign.name[:22]:22} {resume.original_filename[:30]:30} v2 {stored} -> {b['outcome']} "
                  f"core {entry['core']} | targets {len(entry['targets'])} proposals {len(entry['proposals'])} "
                  f"paraphrase calls {entry['llm_calls_paraphrase']}", flush=True)
    finally:
        db.rollback()
        db.close()
    Path(args.out).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print("written", args.out)


if __name__ == "__main__":
    main()
