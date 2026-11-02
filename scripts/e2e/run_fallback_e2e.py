"""
Retire v1, phases A and B, end to end - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_fallback_e2e

The stack's own worker does the scoring with the stub LLM (scripts/e2e/stub_llm.py, every call in
llm_call_log); this driver uses the real API routes as the e2e users, switches the stub's mode, sets
platform settings in the isolated database, and runs the scheduled jobs in-process. No email: any
SES client created in this process fails the run, and the worker only ever logs.

Phase A
  A1  provider outage, last-attempt fallback switched off (e2e worker only): retries with backoff,
      then a dead letter; the outage ends; replay through the API succeeds and resolves it
  A2  provider outage as in production: retries with backoff, then the no-LLM fallback on the last
      attempt (HOLD awaiting full scoring)
  A3  a candidate stuck in Screening (its scoring died; added 2 h ago): the recovery job re-queues it
Phase B
  B1  daily cap reached: no-LLM fallback at once, HOLD "awaiting full scoring"
  B2  the cap resets: the hourly scan re-scores and applies the full outcome
  B3  as B1, then a recruiter moves the candidate; after the reset the full result is recorded only

Report: /data/storage/e2e_fallback_report.json (every step's stages, result rows, audit entries,
task logs, dead letters and LLM calls).
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.run_reparse_e2e import EmailAttempted, Report, _token_payload
from scripts.e2e.setup_e2e import CAMPAIGNS, FIXTURES, assert_isolated

REPORT = Path("/data/storage/e2e_fallback_report.json")
STORAGE = Path("/data/storage")
# A synthetic resume for this JD: 14 years, the four core skills named on an experience line that
# doesn't lead with a work verb - found by the scan, but not auto-confirmed, so they need votes.
# Each scenario uploads its own copy under a different name: a different resume text, so the
# confirmation cache (same text + core skills -> the earlier votes, no call) can't answer for it.
RESUME_LINES = [
    "{name}",
    "PROFILE",
    "Transformation lead for enterprise AI programmes across banking and insurance.",
    "CORE COMPETENCIES",
    "Stakeholder Management | Risk Management | Agile Delivery | Vendor Management",
    "PROFESSIONAL EXPERIENCE",
    "Head of AI Transformation - Northwind Bank",
    "2018 - Present",
    "Scope: AI Governance, AI Transformation, Change Management and Program Management for a 40-person office.",
    "Programme Manager - Contoso Insurance",
    "2012 - 2018",
    "Portfolio of 12 workstreams; budgets up to 4 million; quarterly steering committees.",
    "EDUCATION",
    "MBA, Business, University of Example, 2011",
]
TIMEOUT = 420
CAP_KEY, RECOVERY_KEY = "SCORING_V2_DAILY_LLM_CALL_CAP", "SCORING_DECISION_RECOVERY_HOURS"


def wait_for(predicate, what: str, timeout: int = TIMEOUT):
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

    with patch("boto3.client", guarded), patch("app.middleware.jwt_middleware.decode_token", _token_payload):
        run()


def run() -> None:  # noqa: C901 - one linear scenario
    from docx import Document
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    import app.models  # noqa: F401
    from app.db.session import SessionLocal
    from app.enums.constants import API_PREFIX
    from app.main import app as api
    from app.models.async_tasks import CeleryTaskLog, DeadLetterQueue, TaskStatus
    from app.models.campaigns import HiringCampaign
    from app.models.candidates import Resume
    from app.models.compliance import AuditLog
    from app.models.config import PlatformConfig
    from app.models.llm_call_log import LLMCallLog
    from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory, PipelineStage
    from app.models.scoring_v2 import CandidateScoringResult
    from app.services.campaign.outcome_overrides import REASON_CODES
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task, recover_undecided_candidates
    from app.tasks.full_scoring_tasks import complete_full_scoring_scan
    from scripts.e2e.stub_llm import set_mode

    report = Report()
    steps: dict[str, dict] = {}
    client = TestClient(api, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    recruiter = {"Authorization": "Bearer e2e:e2e-recruiter"}
    db = SessionLocal()
    manifest = json.loads((FIXTURES / "candidates.json").read_text("utf-8"))

    def fresh(model, key):
        db.expire_all()
        return db.get(model, key)

    def set_config(key, value):
        row = db.execute(select(PlatformConfig).where(PlatformConfig.key == key)).scalar_one_or_none()
        if row is None:
            db.add(PlatformConfig(id=uuid.uuid4(), key=key, value=str(value), description="e2e"))
        else:
            row.value = str(value)
        db.commit()

    def campaign(name):
        return db.execute(select(HiringCampaign).where(HiringCampaign.name == name)).scalar_one()

    def clone_campaign(source, name):
        existing = db.execute(select(HiringCampaign).where(HiringCampaign.name == name)).scalar_one_or_none()
        if existing:
            return existing
        values = {c.key: getattr(source, c.key) for c in HiringCampaign.__table__.columns
                  if c.key not in ("id", "name", "created_at", "updated_at")}
        row = HiringCampaign(id=uuid.uuid4(), name=name, **values)
        db.add(row)
        db.commit()
        return row

    def upload(target, label):
        if label.startswith("F"):
            name = f"Test Candidate {label}"
            path = STORAGE / f"e2e_candidate_{label.lower()}.docx"
            document = Document()
            for line in RESUME_LINES:
                document.add_paragraph(line.format(name=name))
            document.save(path)
            info = {"file": path.name, "full_name": name, "email": f"candidate.{label.lower()}@e2e.invalid"}
            content = path.read_bytes()
        else:
            info = manifest[label]
            content = (FIXTURES / info["file"]).read_bytes()
        data = {"campaign_id": str(target.id), "candidate_full_name": info["full_name"],
                "candidate_email": info["email"], "consent_confirmed": "true"}
        response = client.post("/resumes", data=data, headers=recruiter, files={
            "file": (info["file"], content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert response.status_code in (200, 201), f"upload {label}: {response.status_code} {response.text[:300]}"
        resume_id = uuid.UUID(str(response.json()["data"]["resume_id"]))
        return wait_for(lambda: cc_for(target, resume_id), f"{label}'s campaign candidate"), resume_id

    def cc_for(target, resume_id):
        db.expire_all()
        return db.execute(select(CampaignCandidate).where(CampaignCandidate.campaign_id == target.id,
                                                          CampaignCandidate.resume_id == resume_id)).scalar_one_or_none()

    def share(target, resume_id, *, stage=PipelineStage.UPLOADED, created_at=None, queue=True):
        resume = fresh(Resume, resume_id)
        cc = CampaignCandidate(campaign_id=target.id, candidate_id=resume.candidate_id, resume_id=resume_id,
                               idempotency_key=f"e2e-fallback-{uuid.uuid4()}", pipeline_stage=stage)
        if created_at:
            cc.created_at = created_at
        db.add(cc)
        db.commit()
        if queue:
            calculate_deterministic_score_task.apply_async(kwargs={"campaign_candidate_id": str(cc.id)})
        return cc

    def logs(cc_id, task_type=None):
        db.expire_all()
        query = select(CeleryTaskLog).where(CeleryTaskLog.campaign_candidate_id == cc_id)
        if task_type:
            query = query.where(CeleryTaskLog.task_type == task_type)
        return list(db.execute(query.order_by(CeleryTaskLog.queued_at)).scalars())

    def screened(cc_id):
        cc = fresh(CampaignCandidate, cc_id)
        return cc if cc and cc.screened_at is not None else None

    def settled(cc_id, stages=(PipelineStage.HOLD, PipelineStage.SHORTLISTED, PipelineStage.REJECTED)):
        cc = fresh(CampaignCandidate, cc_id)
        return cc if cc and cc.pipeline_stage in stages else None

    def snapshot(step, cc_ids, since, note=""):
        db.expire_all()
        out = {"note": note, "candidates": {}}
        for cc_id in cc_ids:
            cc = db.get(CampaignCandidate, cc_id)
            history = db.execute(select(CampaignCandidateStageHistory)
                                 .where(CampaignCandidateStageHistory.campaign_candidate_id == cc_id)
                                 .order_by(CampaignCandidateStageHistory.changed_at)).scalars()
            results = db.execute(select(CandidateScoringResult)
                                 .where(CandidateScoringResult.campaign_candidate_id == cc_id)
                                 .order_by(CandidateScoringResult.created_at)).scalars()
            dlq = db.execute(select(DeadLetterQueue).where(DeadLetterQueue.campaign_candidate_id == cc_id)).scalars()
            dlq = list(dlq)
            audit_ids = [cc_id, *[d.id for d in dlq]]
            audits = db.execute(select(AuditLog).where(AuditLog.entity_id.in_(audit_ids), AuditLog.created_at >= since)
                                .order_by(AuditLog.created_at)).scalars()
            breakdown = cc.deterministic_breakdown or {}
            out["candidates"][str(cc_id)] = {
                "stage": cc.pipeline_stage.value,
                "stages": [f"{(h.from_stage.value if h.from_stage else '-')}->{h.to_stage.value} "
                           f"({h.transition_source.value}{', by ' + h.changed_by if h.changed_by else ''})"
                           for h in history],
                "needs_review_reason": breakdown.get("needs_review_reason"),
                "awaiting_full_scoring": breakdown.get("awaiting_full_scoring"),
                "results": [{"version": r.scoring_version, "mode": r.scoring_mode, "outcome": r.outcome,
                             "acted_on": r.acted_on, "awaiting": r.awaiting_full_scoring,
                             **({"applied": (r.breakdown or {}).get("applied"),
                                 "effect": (r.breakdown or {}).get("effect")}
                                if "applied" in (r.breakdown or {}) else {}),
                             **({"fallback_reason": (r.breakdown or {}).get("fallback_reason")}
                                if r.scoring_mode == "no_llm" else {})} for r in results],
                "task_logs": [f"{t.task_type}:{t.status.value}" + (f" (retries {t.retry_count})" if t.retry_count else "")
                              for t in logs(cc_id)],
                "dead_letters": [{"task_type": d.task_type, "replayed": d.replayed_at is not None,
                                  "resolved": d.resolved_at is not None} for d in dlq],
                "audit": [f"{a.action_type.value} ({a.actor_role or 'SYSTEM'})" for a in audits],
            }
        calls = db.execute(select(LLMCallLog.source, LLMCallLog.purpose, LLMCallLog.outcome, func.count())
                           .where(LLMCallLog.created_at >= since)
                           .group_by(LLMCallLog.source, LLMCallLog.purpose, LLMCallLog.outcome)).all()
        out["llm_calls"] = {f"{s or '-'}/{p or '-'}/{o}": n for s, p, o, n in calls}
        steps[step] = out
        print(f"--- {step}: " + json.dumps(out, default=str)[:1500], flush=True)
        return out

    # Setup in the isolated database: v2 decides on every campaign used, generous cap, the stub OK.
    campaign_a, campaign_b = campaign(CAMPAIGNS["A"]), campaign(CAMPAIGNS["B"])
    campaign_c = clone_campaign(campaign_a, "[E2E] fallback - apply")
    campaign_d = clone_campaign(campaign_a, "[E2E] fallback - recruiter moves")
    campaign_e = clone_campaign(campaign_a, "[E2E] fallback - outage")
    for target in (campaign_a, campaign_b, campaign_c, campaign_d, campaign_e):
        target.scoring_v2_enabled = True
    db.commit()
    set_config(CAP_KEY, 1000)
    set_mode(confirm="ok", last_attempt_fallback=True)

    # ---------------------------------------------------------------- phase A
    t = datetime.now(timezone.utc)
    set_mode(confirm="outage", last_attempt_fallback=False)
    v_cc, f_resume = upload(campaign_a, "F1")
    wait_for(lambda: any(lg.status == TaskStatus.DEAD for lg in logs(v_cc.id, "DETERMINISTIC_SCORE")),
             "F's scoring to be dead-lettered")
    a1 = snapshot("A1a outage -> retries -> dead letter", [v_cc.id], t,
                  "stub outage on core-skill votes; e2e worker's last-attempt fallback off")
    states = a1["candidates"][str(v_cc.id)]
    report.check(1, "outage: retried with backoff, then dead-lettered (no decision, never rejected)",
                 any(lg.status == TaskStatus.DEAD and (lg.retry_count or 0) >= 3
                     for lg in logs(v_cc.id, "DETERMINISTIC_SCORE"))                  # 4 attempts, one log row
                 and states["dead_letters"] == [{"task_type": "DETERMINISTIC_SCORE", "replayed": False, "resolved": False}]
                 and fresh(CampaignCandidate, v_cc.id).pipeline_stage != PipelineStage.REJECTED
                 and a1["llm_calls"].get("skill_confirmation/-/UNAVAILABLE", 0) > 0,
                 f"logs {states['task_logs']}, calls {a1['llm_calls']}")

    t = datetime.now(timezone.utc)
    set_mode(confirm="ok", last_attempt_fallback=True)
    dlq_id = db.execute(select(DeadLetterQueue.id).where(DeadLetterQueue.campaign_candidate_id == v_cc.id)).scalar_one()
    response = client.post(f"/campaigns/{campaign_a.id}/dead-letter-queue/replay", json={"dlq_ids": [str(dlq_id)]},
                           headers=recruiter)
    replayed = response.status_code == 200
    wait_for(lambda: screened(v_cc.id), "F's replayed scoring")
    wait_for(lambda: fresh(DeadLetterQueue, dlq_id).resolved_at is not None, "the dead letter to resolve")
    a1b = snapshot("A1b outage over -> replay -> resolved", [v_cc.id], t)
    report.check(2, "replay through the API succeeded and resolved the dead letter",
                 replayed and a1b["candidates"][str(v_cc.id)]["dead_letters"][0]["resolved"]
                 and a1b["llm_calls"].get("skill_confirmation/-/ok", 0) > 0,
                 f"HTTP {response.status_code}; stage {fresh(CampaignCandidate, v_cc.id).pipeline_stage.value}")

    t = datetime.now(timezone.utc)
    set_mode(confirm="outage", last_attempt_fallback=True)
    k_cc, _ = upload(campaign_e, "F2")
    wait_for(lambda: screened(k_cc.id), "F's scoring to land (outage campaign)")
    wait_for(lambda: settled(k_cc.id), "F's pipeline to settle (outage campaign)", timeout=300)
    set_mode(confirm="ok")
    a2 = snapshot("A2 outage as in production -> last-attempt fallback", [k_cc.id], t)
    k_rows = a2["candidates"][str(k_cc.id)]["results"]
    acted = [r for r in k_rows if r["version"] == "v2" and r["acted_on"]]
    report.check(3, "outage on every attempt: the last one falls back to no-LLM scoring (never rejects on core)",
                 bool(acted) and acted[-1]["mode"] == "no_llm" and acted[-1]["outcome"] in ("REVIEW", "REJECT")
                 and any((lg.retry_count or 0) >= 3 for lg in logs(k_cc.id, "DETERMINISTIC_SCORE")),
                 f"acted v2 row {acted[-1] if acted else None}; stage {a2['candidates'][str(k_cc.id)]['stage']}")

    t = datetime.now(timezone.utc)
    set_config(RECOVERY_KEY, 1)
    stuck = share(campaign_b, f_resume, stage=PipelineStage.SCREENING,
                  created_at=datetime.now(timezone.utc) - timedelta(hours=2), queue=False)
    db.add(CeleryTaskLog(task_id=f"e2e-dead-run-{uuid.uuid4()}", task_type="DETERMINISTIC_SCORE",
                         status=TaskStatus.FAILURE, campaign_candidate_id=stuck.id,
                         queued_at=datetime.now(timezone.utc) - timedelta(hours=2),
                         completed_at=datetime.now(timezone.utc) - timedelta(hours=2), error_message="e2e: worker died"))
    db.commit()
    recover_undecided_candidates.apply(throw=True)
    wait_for(lambda: screened(stuck.id), "the stuck candidate to be scored")
    a3 = snapshot("A3 stuck in Screening -> recovery job", [stuck.id], t)
    scan = db.execute(select(CeleryTaskLog).where(CeleryTaskLog.task_type == "SCORING_DECISION_RECOVERY_SCAN")
                      .order_by(CeleryTaskLog.queued_at.desc())).scalars().first()
    report.check(4, "the recovery job re-queued the candidate stuck in Screening and its decision landed",
                 '"requeued": 1' in (scan.output_summary or "") and screened(stuck.id) is not None,
                 f"scan {scan.output_summary}; logs {a3['candidates'][str(stuck.id)]['task_logs']}")

    # ---------------------------------------------------------------- phase B
    t = datetime.now(timezone.utc)
    set_config(CAP_KEY, 0)
    c_cc, _ = upload(campaign_c, "F3")
    wait_for(lambda: settled(c_cc.id), "the capped candidate to settle")
    b1 = snapshot("B1 daily cap reached -> no-LLM fallback -> HOLD awaiting", [c_cc.id], t)
    c_state = b1["candidates"][str(c_cc.id)]
    report.check(5, "cap reached: fallback at once (no retry, no vote), HOLD awaiting full scoring",
                 c_state["stage"] == "HOLD" and c_state["awaiting_full_scoring"] is True
                 and (c_state["needs_review_reason"] or "").startswith("Awaiting full scoring")
                 and not any(k.startswith("skill_confirmation") for k in b1["llm_calls"])
                 and all(not lg.retry_count for lg in logs(c_cc.id, "DETERMINISTIC_SCORE")),
                 f"stage {c_state['stage']}, reason {c_state['needs_review_reason']}, calls {b1['llm_calls']}")

    t = datetime.now(timezone.utc)
    set_config(CAP_KEY, 1000)
    complete_full_scoring_scan.apply(throw=True)
    wait_for(lambda: any(lg.status == TaskStatus.SUCCESS for lg in logs(c_cc.id, "FULL_SCORING")),
             "the full scoring of the capped candidate")
    wait_for(lambda: any(lg.status == TaskStatus.SUCCESS for lg in logs(k_cc.id, "FULL_SCORING")),
             "the full scoring of A2's outage candidate")
    b2 = snapshot("B2 cap resets -> scan -> full scoring applied", [c_cc.id, k_cc.id], t)
    c_rows = b2["candidates"][str(c_cc.id)]["results"]
    full = [r for r in c_rows if r.get("applied") is not None]
    report.check(6, "after the reset the scan re-scored it and applied the full outcome (nobody had acted)",
                 bool(full) and full[-1]["applied"] is True and full[-1]["acted_on"] is True
                 and not any(r["awaiting"] for r in c_rows)
                 and b2["llm_calls"].get("skill_confirmation/-/ok", 0) > 0,
                 f"full {full[-1] if full else None}; stage {b2['candidates'][str(c_cc.id)]['stage']}")

    t = datetime.now(timezone.utc)
    set_config(CAP_KEY, 0)
    d_cc, _ = upload(campaign_d, "F4")
    wait_for(lambda: settled(d_cc.id), "the second capped candidate to settle")
    code = next(iter(REASON_CODES))
    move = client.post(f"/candidate-actions/campaigns/{campaign_d.id}/candidates/{d_cc.id}/stage-move",
                       json={"target_stage": "SHORTLISTED", "reason": "E2E: recruiter reviewed the candidate",
                             "override_reason_code": code}, headers=recruiter)
    set_config(CAP_KEY, 1000)
    complete_full_scoring_scan.apply(throw=True)
    wait_for(lambda: any(lg.status == TaskStatus.SUCCESS for lg in logs(d_cc.id, "FULL_SCORING")),
             "the full scoring of the moved candidate")
    b3 = snapshot("B3 recruiter moved it in between -> full result recorded only", [d_cc.id], t,
                  f"recruiter stage-move HTTP {move.status_code}")
    d_state = b3["candidates"][str(d_cc.id)]
    d_full = [r for r in d_state["results"] if r.get("applied") is not None]
    report.check(7, "a recruiter moved the candidate first: full result recorded, stage untouched",
                 move.status_code == 200 and d_state["stage"] == "SHORTLISTED" and bool(d_full)
                 and d_full[-1]["applied"] is False and d_full[-1]["acted_on"] is False,
                 f"move HTTP {move.status_code} {move.text[:160] if move.status_code != 200 else ''}; "
                 f"stage {d_state['stage']}; full {d_full[-1] if d_full else None}")

    report.check(8, "no email: no SES client created in this process", True)
    REPORT.write_text(json.dumps({"checks": report.rows, "steps": steps}, indent=1, default=str), "utf-8")
    print(f"report: {REPORT}")
    db.close()
    if any(r["result"] == "FAIL" for r in report.rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
