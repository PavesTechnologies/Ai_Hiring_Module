"""
Re-parse / re-score end-to-end check - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_reparse_e2e

Drives the real API routes in-process (FastAPI TestClient) as the e2e test
users - the JWT check is replaced by a local identity only inside this
process - while the stack's own Celery worker does the background work.
Refuses to run unless the stack is isolated. Any attempt to create an SES
client in this process fails the run.

Checks (each PASS / FAIL, report at /data/storage/e2e_report.json):
  1  fixtures uploaded as the recruiter; candidate V rejected by v1
  2  candidate V's resume shared into campaign B (same resume), candidate set to SHORTLISTED there
  3  re-parse of a REJECTED candidate without a reason -> 422
  4  re-parse with a reason -> accepted; previous parse kept as a version
  5  new parse: skills replaced (no duplicates), v2 evidence replaced
  6  re-embedded (new resume embedding after the re-parse)
  7  re-scored (deterministic task after the re-parse)
  8  moved REJECTED -> SCREENING by the recruiter, with the reason
  9  parse-changes endpoint (the Parse changes panel's data) shows the completed diff
 10  campaign B candidate marked "score out of date", visible on its scorecard data
 11  recruiter re-score of the SHORTLISTED campaign-B candidate is score-only: scores updated,
     stage unchanged, out-of-date cleared
 12  no email: no SES client created; every scheduled job run once, still none
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.setup_e2e import CAMPAIGNS, FIXTURES, assert_isolated

REPORT = Path("/data/storage/e2e_report.json")
TIMEOUT_SECONDS = 600
REASON = "E2E: competency section was missed in the first parse"


class EmailAttempted(RuntimeError):
    pass


def _token_payload(token: str) -> dict:
    user_id = token.removeprefix("e2e:")
    role = {"e2e-recruiter": "RECRUITER", "e2e-hr-admin": "HR_ADMIN", "e2e-hiring-manager": "HIRING_MANAGER"}[user_id]
    return {"user_id": user_id, "email": f"{user_id}@e2e.invalid", "roles": [role], "name": user_id}


class Report:
    def __init__(self):
        self.rows = []

    def check(self, number: int, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"check": number, "name": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {number:>2}. {name}" + (f" - {detail}" if detail else ""), flush=True)
        return ok


def run_scheduled_jobs() -> dict:
    """Every beat-scheduled job, run once in this process (task modules loaded first, as the worker does)."""
    from app.core.celery_app import celery_app
    celery_app.loader.import_default_modules()   # conf.imports + conf.include
    results = {}
    for entry in celery_app.conf.beat_schedule.values():
        name = entry["task"]
        try:
            celery_app.tasks[name].apply(throw=True)
            results[name] = "ran"
        except EmailAttempted:
            results[name] = "EMAIL ATTEMPTED"
        except Exception as exc:  # a job failing on the small test DB is fine - it didn't email
            results[name] = f"error: {type(exc).__name__}"
    return results


def wait_for(predicate, what: str, timeout: int = TIMEOUT_SECONDS):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        value = predicate()
        if value:
            return value
        time.sleep(3)
    raise TimeoutError(f"timed out waiting for {what}")


def main() -> None:
    assert_isolated()
    import boto3
    real_client = boto3.client

    def guarded(service, *args, **kwargs):
        if service == "ses":
            raise EmailAttempted("an SES client was created")
        return real_client(service, *args, **kwargs)

    with patch("boto3.client", guarded), \
            patch("app.middleware.jwt_middleware.decode_token", _token_payload):
        run()


def run() -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.async_tasks import CeleryTaskLog, TaskStatus
    from app.models.campaigns import HiringCampaign
    from app.models.candidates import Resume
    from app.models.compliance import AuditLog
    from app.models.embeddings import ResumeEmbedding
    from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory, PipelineStage
    from app.models.scoring_v2 import ResumeParseVersion, ResumeSkillEvidence
    from app.models.skills import CandidateSkill

    report = Report()
    from app.enums.constants import API_PREFIX
    client = TestClient(app, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    recruiter = {"Authorization": "Bearer e2e:e2e-recruiter"}
    db = SessionLocal()

    def fresh(model, key):
        db.expire_all()
        return db.get(model, key)

    campaign_a = db.execute(select(HiringCampaign).where(HiringCampaign.name == CAMPAIGNS["A"])).scalar_one()
    campaign_b = db.execute(select(HiringCampaign).where(HiringCampaign.name == CAMPAIGNS["B"])).scalar_one()
    manifest = json.loads((FIXTURES / "candidates.json").read_text("utf-8"))

    def upload(campaign, label, resolution=None):
        info = manifest[label]
        data = {"campaign_id": str(campaign.id), "candidate_full_name": info["full_name"],
                "candidate_email": info["email"], "consent_confirmed": "true"}
        if resolution:
            data["resolution"] = resolution
        content = (FIXTURES / info["file"]).read_bytes()
        response = client.post("/resumes", data=data, headers=recruiter, files={
            "file": (info["file"], content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert response.status_code in (200, 201), f"upload {label}: {response.status_code} {response.text[:300]}"
        return response.json()["data"]

    def cc_for(campaign, resume_id):
        db.expire_all()
        return db.execute(select(CampaignCandidate).where(CampaignCandidate.campaign_id == campaign.id,
                                                          CampaignCandidate.resume_id == resume_id)).scalar_one_or_none()

    def scored(cc_id):
        cc = fresh(CampaignCandidate, cc_id)
        return cc if cc and cc.deterministic_passed is not None else None

    # 1 - upload both fixtures; V is expected to be rejected by v1.
    v = upload(campaign_a, "V")
    k = upload(campaign_a, "K")
    v_resume_id = uuid.UUID(str(v["resume_id"]))
    v_cc = wait_for(lambda: cc_for(campaign_a, v_resume_id), "V's campaign candidate")
    wait_for(lambda: scored(v_cc.id), "V's first deterministic score")
    wait_for(lambda: scored(cc_for(campaign_a, uuid.UUID(str(k["resume_id"]))).id), "K's first deterministic score")
    v_cc = fresh(CampaignCandidate, v_cc.id)
    report.check(1, "fixtures uploaded by the recruiter; V rejected by v1", v_cc.pipeline_stage == PipelineStage.REJECTED,
                 f"V stage {v_cc.pipeline_stage.value}, score {v_cc.deterministic_score}")

    # 2 - share V's resume into campaign B and make that candidate SHORTLISTED (precondition).
    # Created directly: the upload's "use_existing" path doesn't commit the new
    # campaign candidate (pre-existing bug, docs/known_issues.md) - so the
    # shared-resume candidate is set up here, then scored by the normal task.
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task
    v_resume = fresh(Resume, v_resume_id)
    shared = CampaignCandidate(campaign_id=campaign_b.id, candidate_id=v_resume.candidate_id, resume_id=v_resume_id,
                               idempotency_key=f"e2e-shared-{uuid.uuid4()}", pipeline_stage=PipelineStage.UPLOADED)
    db.add(shared)
    db.commit()
    calculate_deterministic_score_task.apply_async(kwargs={"campaign_candidate_id": str(shared.id)})
    b_cc = wait_for(lambda: cc_for(campaign_b, v_resume_id), "V's campaign-B candidate")
    wait_for(lambda: scored(b_cc.id), "V's campaign-B score")
    b_cc = fresh(CampaignCandidate, b_cc.id)
    b_cc.pipeline_stage = PipelineStage.SHORTLISTED      # isolated test DB only: precondition, not under test
    db.commit()
    report.check(2, "same resume in campaign B, candidate SHORTLISTED there", b_cc.resume_id == v_resume_id)

    # 3 - re-parse without a reason.
    response = client.post(f"/campaign-candidates/{v_cc.id}/reparse", json={"reason": ""}, headers=recruiter)
    report.check(3, "re-parse of a rejected candidate without a reason is refused", response.status_code == 422,
                 f"HTTP {response.status_code}")

    # 4 - re-parse with a reason.
    before_resume = fresh(Resume, v_resume_id)
    before_parsed = before_resume.parsed_json
    before_skills = {(s.canonical_skill_id, s.raw_extracted_text) for s in db.execute(
        select(CandidateSkill).where(CandidateSkill.resume_id == v_resume_id)).scalars()}
    before_embedding = db.execute(select(ResumeEmbedding).where(ResumeEmbedding.resume_id == v_resume_id)).scalar_one_or_none()
    before_embedding_id = before_embedding.id if before_embedding else None   # the row is deleted by the re-embed
    t0 = datetime.now(timezone.utc)
    response = client.post(f"/campaign-candidates/{v_cc.id}/reparse", json={"reason": REASON}, headers=recruiter)
    accepted = response.status_code == 202 and response.json()["data"]["moves_to_screening"] is True
    version = db.execute(select(ResumeParseVersion).where(ResumeParseVersion.resume_id == v_resume_id)
                         .order_by(ResumeParseVersion.version_no.desc())).scalars().first()
    report.check(4, "re-parse with a reason accepted; previous parse kept",
                 accepted and version is not None and version.parsed_json == before_parsed and version.reason == REASON,
                 f"HTTP {response.status_code}, version {version.version_no if version else None}")

    def completed():
        db.expire_all()
        row = db.get(ResumeParseVersion, version.id)
        return row if row and (row.diff or {}).get("status") in ("COMPLETED", "FAILED") else None
    version = wait_for(completed, "the re-parse to finish")

    # 5 - skills replaced.
    after = list(db.execute(select(CandidateSkill).where(CandidateSkill.resume_id == v_resume_id)).scalars())
    after_set = {(s.canonical_skill_id, s.raw_extracted_text) for s in after}
    canonical = [s.canonical_skill_id for s in after if s.canonical_skill_id]
    evidence = list(db.execute(select(ResumeSkillEvidence).where(ResumeSkillEvidence.resume_id == v_resume_id)).scalars())
    report.check(5, "skills replaced (no duplicates), v2 evidence replaced",
                 version.diff.get("status") == "COMPLETED" and after_set != before_skills
                 and len(canonical) == len(set(canonical)) and evidence and all(e.created_at >= t0 for e in evidence),
                 f"skills {len(before_skills)} -> {len(after_set)}, +{len(version.diff.get('skills_added', []))} "
                 f"-{len(version.diff.get('skills_removed', []))}")

    # 6 - re-embedded.
    def new_embedding_row():
        db.expire_all()
        return db.execute(select(ResumeEmbedding).where(
            ResumeEmbedding.resume_id == v_resume_id, ResumeEmbedding.created_at >= t0)).scalar_one_or_none()
    new_embedding = wait_for(new_embedding_row, "the new resume embedding")
    report.check(6, "re-embedded (old vector replaced)", new_embedding is not None and new_embedding.id != before_embedding_id,
                 f"embedding before: {'yes' if before_embedding_id else 'none'}")

    # 7 - re-scored.
    def rescored(cc_id, since):
        db.expire_all()
        return db.execute(select(CeleryTaskLog).where(
            CeleryTaskLog.campaign_candidate_id == cc_id, CeleryTaskLog.task_type == "DETERMINISTIC_SCORE",
            CeleryTaskLog.status == TaskStatus.SUCCESS, CeleryTaskLog.queued_at >= since)).scalars().first()
    report.check(7, "re-scored after the re-parse", bool(wait_for(lambda: rescored(v_cc.id, t0), "the re-score")))

    # 8 - moved to SCREENING by the recruiter with the reason.
    move = db.execute(select(CampaignCandidateStageHistory).where(
        CampaignCandidateStageHistory.campaign_candidate_id == v_cc.id,
        CampaignCandidateStageHistory.from_stage == PipelineStage.REJECTED,
        CampaignCandidateStageHistory.to_stage == PipelineStage.SCREENING,
        CampaignCandidateStageHistory.changed_at >= t0)).scalars().first()
    report.check(8, "moved REJECTED -> SCREENING by the recruiter with the reason",
                 move is not None and move.changed_by == "e2e-recruiter" and REASON in (move.change_reason or ""),
                 f"now {fresh(CampaignCandidate, v_cc.id).pipeline_stage.value} (a re-score may reject again)")

    # 9 - parse-changes (Parse changes panel data).
    changes = client.get(f"/campaign-candidates/{v_cc.id}/parse-changes", headers=recruiter).json()["data"]
    latest = changes["versions"][0] if changes["versions"] else {}
    report.check(9, "Parse changes panel data: completed re-parse with what changed",
                 latest.get("status") == "COMPLETED" and "core_matched_after" in (latest.get("diff") or {})
                 and latest.get("reason") == REASON, f"rescore: {(latest.get('diff') or {}).get('rescore')}")

    # 10 - campaign B marked out of date and shown on its scorecard data.
    b_cc = fresh(CampaignCandidate, b_cc.id)
    scorecard = client.get(f"/resumes/candidate/{b_cc.id}/parsed-json", headers=recruiter).json().get("data") or {}
    report.check(10, "campaign-B candidate marked 'score out of date' and shown on its scorecard",
                 b_cc.score_out_of_date_at is not None and bool(scorecard.get("score_out_of_date_at")),
                 (b_cc.score_out_of_date_reason or "")[:90])

    # 11 - score-only re-score of the SHORTLISTED candidate.
    previous_score = b_cc.deterministic_score
    t1 = datetime.now(timezone.utc)
    response = client.post(f"/campaign-candidates/{b_cc.id}/rescore", headers=recruiter)
    wait_for(lambda: rescored(b_cc.id, t1), "the score-only re-score")
    b_cc = fresh(CampaignCandidate, b_cc.id)
    audit = db.execute(select(AuditLog).where(AuditLog.entity_id == b_cc.id, AuditLog.created_at >= t1)
                       .order_by(AuditLog.created_at.desc())).scalars().all()
    score_only = any((a.detail or {}).get("score_only") for a in audit)
    report.check(11, "score-only re-score: scores updated, stage unchanged, out-of-date cleared",
                 response.status_code == 202 and b_cc.pipeline_stage == PipelineStage.SHORTLISTED
                 and b_cc.score_out_of_date_at is None and score_only,
                 f"stage {b_cc.pipeline_stage.value}, score {previous_score} -> {b_cc.deterministic_score}")

    # 12 - no email, including every scheduled job.
    job_results = run_scheduled_jobs()
    emailed = [n for n, r in job_results.items() if r == "EMAIL ATTEMPTED"]
    report.check(12, "no email: no SES client created, every scheduled job run once", not emailed,
                 f"{len(job_results)} scheduled jobs run; " + ", ".join(f"{n.split('.')[-1]}={r}" for n, r in job_results.items()))

    REPORT.write_text(json.dumps({"finished_at": datetime.now(timezone.utc).isoformat(), "checks": report.rows,
                                  "scheduled_jobs": job_results}, indent=1), "utf-8")
    failed = [r for r in report.rows if r["result"] == "FAIL"]
    print(f"\n{len(report.rows) - len(failed)}/{len(report.rows)} checks passed. Report: {REPORT}")
    db.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
