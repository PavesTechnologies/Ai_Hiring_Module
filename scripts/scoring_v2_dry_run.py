"""
Scoring v2 dry run for a campaign: v1 next to v2, per candidate. Writes
nothing to the DB (the session is rolled back).

v1 is recomputed now with the current rules (25% missing-core limit, score
threshold not rejecting when the JD has core skills) - the stored v1 result
may predate those rules - so the comparison is fair. The stored decision is
shown too. v2's outcome is PASS / REVIEW (would_be_review) / REJECT.

Uses each resume's v2 evidence (from resume_skill_evidence, or detected now
from the stored file + stored parse) and runs the core-skill confirmation
with the active provider, uncached, --repeat times - so you can see whether
the v2 outcome is stable.

    python -m scripts.scoring_v2_dry_run --campaign "AI transformation lead"
    python -m scripts.scoring_v2_dry_run --campaign "..." --repeat 3
    python -m scripts.scoring_v2_dry_run --campaign "..." --no-llm      # every core skill NOT_RUN
"""
import argparse
import logging
import re
import sys

logging.disable(logging.WARNING)

from app.db.session import SessionLocal, engine  # noqa: E402

engine.echo = False
import app.models  # noqa: E402,F401
from app.models.campaigns import HiringCampaign  # noqa: E402
from app.models.candidates import Resume  # noqa: E402
from app.models.pipeline import CampaignCandidate  # noqa: E402
from app.repositories.config_repository import ConfigRepository  # noqa: E402
from app.repositories.skill_repository import SkillRepository  # noqa: E402
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository  # noqa: E402
from app.repositories.skill_ontology_repository import SkillOntologyRepository  # noqa: E402
from app.services.campaign.candidate_scoring_service import CandidateScoringService, _optional_float  # noqa: E402
from app.services.campaign.scoring_v2_service import ScoringV2Service  # noqa: E402
from app.services.llm.factory import resolve_active_provider  # noqa: E402
from app.services.llm.model_selection import LLMPurpose  # noqa: E402
from app.tasks.deterministic_scoring_tasks import build_validation_inputs  # noqa: E402


class _NoCacheRepo:
    """Detection repository wrapper: never reads/writes confirmation cache (each repeat asks again)."""

    def __init__(self, inner):
        self._inner = inner

    def get_confirmation(self, key):
        return None

    def put_confirmation(self, row):
        return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _label(resume) -> str:
    stem = re.sub(r"\.[a-z]+$", "", resume.original_filename or str(resume.id), flags=re.I)
    return re.sub(r"[_\[\]()]+", " ", stem.split("_", 1)[-1]).strip()[:24]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--repeat", type=int, default=1, help="confirmation repeats per candidate (default 1)")
    parser.add_argument("--no-llm", action="store_true", help="skip the core confirmation (all NOT_RUN)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        campaign = db.query(HiringCampaign).filter(HiringCampaign.name.ilike(f"%{args.campaign}%")).first()
        if campaign is None:
            raise SystemExit(f"No campaign matching {args.campaign!r}")
        jd = campaign.job_description
        config_repo = ConfigRepository(db)
        service = ScoringV2Service(
            db, SkillRepository(db), config_repo,
            provider_resolver=None if args.no_llm else (lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION)),
        )
        service.detection_repository = _NoCacheRepo(service.detection_repository)
        print(f"Campaign: {campaign.name} | threshold {campaign.deterministic_threshold} | "
              f"scoring_v2_enabled={campaign.scoring_v2_enabled}\n")
        v1_service = CandidateScoringService(SkillRepository(db), SkillOntologyRepository(db), config_repo,
                                             CampaignCandidateRepository(db))
        print(f"{'candidate':24} {'v1 stored':>9} | {'v1 now':>6} {'':>6} {'core':>4} | {'v2':>6} {'':>6} {'core':>4}"
              "  confirmed / uncertain / found-not-confirmed / rejected / sense-mismatch")
        rows = (db.query(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                .filter(CampaignCandidate.campaign_id == campaign.id).order_by(Resume.original_filename).all())
        for cc, resume in rows:
            stored = "PASS" if cc.deterministic_passed else "REJECT"
            experience, education, weights, domain = build_validation_inputs(config_repo, jd, resume)
            v1 = v1_service.calculate_and_store_score_breakdown(
                cc.id, campaign.jd_id, cc.resume_id, float(campaign.deterministic_threshold),
                experience_result=experience, education_result=education, score_weights=weights,
                required_skill_coverage_threshold=float(campaign.required_skill_coverage_threshold),
                max_missing_core_skills=int(campaign.max_missing_core_skills),
                max_missing_core_pct=_optional_float(getattr(campaign, "max_missing_core_pct", None)),
                domain_result=domain,
            )
            v1_core = sum(1 for e in v1["mandatory_skills"] if e.get("importance") == "core" and e["match_type"] != "MISSING")
            v1_total = sum(1 for e in v1["mandatory_skills"] if e.get("importance") == "core")
            v1_cols = (f"{stored:>9} | {v1['deterministic_score']:6.2f} {'PASS' if v1['deterministic_passed'] else 'REJECT':>6} "
                       f"{v1_core}/{v1_total}")
            for rep in range(args.repeat):
                b = service.score(
                    campaign=campaign, campaign_candidate=cc, job_description=jd, resume=resume,
                    experience_result=experience, education_result=education, domain_result=domain,
                    acted_on=False, use_llm=not args.no_llm, persist=False,
                )
                core = f"{b['core_confirmed_count']}/{b['core_skill_count']}"
                outcome = "REVIEW" if b["would_be_review"] else b["outcome"]
                lists = " / ".join(", ".join(b.get(k) or []) or "-" for k in (
                    "core_confirmed", "core_uncertain", "core_found_not_confirmed", "core_rejected_by_llm",
                    "core_sense_mismatch"))
                name = _label(resume) if rep == 0 else ""
                left = v1_cols if rep == 0 else f"{'':>9} | {'':6} {'':>6} {'':4}"
                print(f"{name:24} {left} | {b['deterministic_score']:6.2f} {outcome:>6} {core:>4}  {lists}"
                      + (f"   failed: {', '.join(b['failed_checks'])}" if b["failed_checks"] else ""), flush=True)
            if args.repeat > 1:
                print()
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
