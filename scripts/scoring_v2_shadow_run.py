"""
Scoring v2 Step 5: turn shadow on for ONE campaign and score its existing
candidates with v2 (stored in candidate_scoring_results - never acted on),
then print the v1 / v2 table. Runs everything in this process.

    python -m scripts.scoring_v2_shadow_run --campaign "AI transformation lead Sep" [--apply]

Without --apply: nothing is written (v2 computed and printed only).
No email, by construction: EMAIL_DELIVERY=log is forced before the app loads
(the SES client is never created), any attempt to create an SES client aborts
the run, and no Celery task is queued (v2 runs inline, not via a worker).
The v1 column is recomputed under today's rules in a separate session that is
rolled back (v1 is never changed); the stored v1 decision is shown too.
"""
import os

os.environ["EMAIL_DELIVERY"] = "log"  # before any app import - standing rule: never send email

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
from unittest.mock import patch  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

logging.disable(logging.WARNING)


class EmailAttempted(RuntimeError):
    pass


def _storage_reachable() -> bool:
    from app.core.config import settings
    if (settings.storage_backend or "").lower() == "local":
        return True
    try:
        socket.getaddrinfo(urlparse(settings.SUPABASE_URL).hostname, 443)
        return True
    except OSError:
        return False


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True, help="exact campaign name")
    parser.add_argument("--apply", action="store_true", help="turn shadow on and store the v2 results")
    parser.add_argument("--out", help="also write the table as JSON")
    args = parser.parse_args()

    import boto3
    real_client = boto3.client

    def guarded(service, *a, **kw):
        if service == "ses":
            raise EmailAttempted("an SES client was created - aborting")
        return real_client(service, *a, **kw)

    with patch("boto3.client", guarded):
        run(args)


def run(args) -> None:
    from app.core.config import settings
    assert settings.email_delivery == "log", "EMAIL_DELIVERY must be log"
    if not _storage_reachable():
        raise SystemExit("Resume storage isn't reachable - v2 needs each resume's text. Nothing was changed.")

    from app.db.session import SessionLocal, engine
    engine.echo = False
    import app.models  # noqa: F401
    from app.models.campaigns import HiringCampaign
    from app.models.candidates import ParseStatus, Resume
    from app.models.pipeline import CampaignCandidate
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    from app.repositories.config_repository import ConfigRepository
    from app.repositories.skill_ontology_repository import SkillOntologyRepository
    from app.repositories.skill_repository import SkillRepository
    from app.services.campaign.candidate_scoring_service import CandidateScoringService, _optional_float
    from app.services.campaign.scoring_shadow_report_service import ScoringShadowReportService
    from app.services.campaign.scoring_v2_service import ScoringV2Service
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    from app.tasks.deterministic_scoring_tasks import build_validation_inputs

    db = SessionLocal()
    scratch = SessionLocal()           # v1 recompute only - always rolled back
    try:
        matches = db.query(HiringCampaign).filter(HiringCampaign.name == args.campaign).all()
        if len(matches) != 1:
            raise SystemExit(f"Expected exactly one campaign named {args.campaign!r}, found {len(matches)}.")
        campaign = matches[0]
        others_on = db.query(HiringCampaign).filter(HiringCampaign.id != campaign.id,
                                                    HiringCampaign.scoring_v2_shadow.is_(True)).count()
        print(f"Campaign: {campaign.name} | shadow currently {'on' if campaign.scoring_v2_shadow else 'off'} | "
              f"other campaigns with shadow on: {others_on} | mode: {'APPLY' if args.apply else 'dry run'}")

        if args.apply and not campaign.scoring_v2_shadow:
            ScoringShadowReportService(db).set_shadow(campaign.id, enabled=True, backfill=False,
                                                      actor_id=None, actor_role="SYSTEM")
            print("Shadow turned on for this campaign only.")

        config_repo = ConfigRepository(db)
        service = ScoringV2Service(db, SkillRepository(db), config_repo,
                                   provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION))
        v1_service = CandidateScoringService(SkillRepository(scratch), SkillOntologyRepository(scratch),
                                             ConfigRepository(scratch), CampaignCandidateRepository(scratch))
        rows = []
        pairs = (db.query(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                 .filter(CampaignCandidate.campaign_id == campaign.id).order_by(Resume.original_filename).all())
        for cc, resume in pairs:
            label = (resume.original_filename or str(resume.id)).split("_", 1)[-1].rsplit(".", 1)[0][:28]
            if resume.parse_status != ParseStatus.PARSED:
                rows.append({"candidate": label, "note": f"resume {resume.parse_status.value} - skipped"})
                continue
            jd = campaign.job_description
            experience, education, weights, domain = build_validation_inputs(config_repo, jd, resume)
            v1 = v1_service.calculate_and_store_score_breakdown(
                cc.id, campaign.jd_id, cc.resume_id, float(campaign.deterministic_threshold),
                experience_result=experience, education_result=education, score_weights=weights,
                required_skill_coverage_threshold=float(campaign.required_skill_coverage_threshold),
                max_missing_core_skills=int(campaign.max_missing_core_skills),
                max_missing_core_pct=_optional_float(getattr(campaign, "max_missing_core_pct", None)),
                domain_result=domain,
            )
            scratch.rollback()
            v2 = service.score(campaign=campaign, campaign_candidate=cc, job_description=jd, resume=resume,
                               experience_result=experience, education_result=education, domain_result=domain,
                               acted_on=False, persist=args.apply)
            if args.apply:
                db.commit()
            outcome = "REVIEW" if v2.get("would_be_review") else v2.get("outcome")
            rows.append({
                "candidate": label, "stage": cc.pipeline_stage.value,
                "v1_stored": "PASS" if cc.deterministic_passed else "REJECT",
                "v1_now_score": v1.get("deterministic_score"),
                "v1_now": "PASS" if v1.get("deterministic_passed") else "REJECT",
                "v2_score": v2.get("deterministic_score"), "v2": outcome,
                "v2_core": f"{v2.get('core_confirmed_count')}/{v2.get('core_skill_count')}",
                "confirmed": v2.get("core_confirmed") or [], "uncertain": v2.get("core_uncertain") or [],
                "rejected": v2.get("core_rejected_by_llm") or [], "sense_mismatch": v2.get("core_sense_mismatch") or [],
                "failed": v2.get("failed_checks") or [], "reason": v2.get("outcome_reason"),
                "llm_calls": (v2.get("confirmation") or {}).get("llm_calls"),
            })
            print(f"  scored {label}: v2 {outcome}", flush=True)
        _print(rows)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=1, default=str)
        if args.apply:
            report = ScoringShadowReportService(db).report(campaign.id)
            print(f"\nShadow report: {len(report['rows'])} rows, {report['disagreement_count']} disagreements, "
                  f"LLM calls today {report['daily_llm_calls_used']}/{report['daily_llm_call_cap']}")
    finally:
        scratch.rollback()
        scratch.close()
        if not args.apply:
            db.rollback()
        db.close()


def _print(rows: list[dict]) -> None:
    print(f"\n{'candidate':28} {'stage':12} {'v1 stored':>9} {'v1 now':>14} {'v2':>15} {'core':>5}  notes")
    for r in rows:
        if "note" in r:
            print(f"{r['candidate']:28} {r['note']}")
            continue
        v1_now = f"{r['v1_now_score']:.2f} {r['v1_now']}" if r["v1_now_score"] is not None else r["v1_now"]
        v2 = f"{r['v2_score']:.2f} {r['v2']}" if r["v2_score"] is not None else r["v2"]
        notes = "; ".join(x for x in (
            "confirmed: " + ", ".join(r["confirmed"]) if r["confirmed"] else "",
            "uncertain: " + ", ".join(r["uncertain"]) if r["uncertain"] else "",
            "not own skill: " + ", ".join(r["rejected"]) if r["rejected"] else "",
            "other meaning: " + ", ".join(r["sense_mismatch"]) if r["sense_mismatch"] else "",
            r["reason"] or "") if x)
        print(f"{r['candidate']:28} {r['stage']:12} {r['v1_stored']:>9} {v1_now:>14} {v2:>15} {r['v2_core']:>5}  {notes}")


if __name__ == "__main__":
    main()
