"""
Retire v1, phase D, end to end - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_scoring_versions_e2e

Shadow mode as "the current scoring version vs a candidate one", through the real API routes as the e2e
users; the stack's own worker scores with the stub LLM. No email: any SES client created in this process
fails the run, and the worker only ever logs. The candidate version here changes a weight only - never a
model (a version's own model goes to the real provider, which this stack must never call).

  D1  the seed's current version exists; HR creates a candidate version; a recruiter can't
  D2  trying it on a campaign with backfill: its candidates get a candidate-version result, never acted
      on, and their decision (stage, results acted on) is untouched
  D3  a new upload: the current version decides (result tagged with it), the candidate one shadows it
  D4  the shadow report compares the two by label
  D5  promotion (audited): the candidate becomes current, the old one retired, the campaign stops
      shadowing; the next decision is tagged with the promoted version
  D6  no email

Report: /data/storage/e2e_scoring_versions_report.json
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.run_fallback_e2e import RESUME_LINES, STORAGE, wait_for
from scripts.e2e.run_reparse_e2e import EmailAttempted, Report, _token_payload
from scripts.e2e.setup_e2e import CAMPAIGNS, assert_isolated

REPORT = Path("/data/storage/e2e_scoring_versions_report.json")
LABEL = f"e2e core x3 {datetime.now(timezone.utc):%Y%m%d%H%M%S}"
CANDIDATE_CONFIG = {"importance_weights": {"core": 3.0}}


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
    from sqlalchemy import select

    import app.models  # noqa: F401
    from app.db.session import SessionLocal
    from app.enums.constants import API_PREFIX
    from app.main import app as api
    from app.models.async_tasks import CeleryTaskLog, TaskStatus
    from app.models.campaigns import HiringCampaign
    from app.models.compliance import AuditLog
    from app.models.pipeline import CampaignCandidate
    from app.models.scoring_v2 import CandidateScoringResult, ScoringVersion
    from scripts.e2e.stub_llm import set_mode

    report = Report()
    steps: dict = {}
    client = TestClient(api, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    recruiter = {"Authorization": "Bearer e2e:e2e-recruiter"}
    hr = {"Authorization": "Bearer e2e:e2e-hr-admin"}
    db = SessionLocal()
    set_mode(confirm="ok", last_attempt_fallback=True)

    def clone_campaign(name):
        existing = db.execute(select(HiringCampaign).where(HiringCampaign.name == name)).scalar_one_or_none()
        if existing:
            return existing
        source = db.execute(select(HiringCampaign).where(HiringCampaign.name == CAMPAIGNS["A"])).scalar_one()
        values = {c.key: getattr(source, c.key) for c in HiringCampaign.__table__.columns
                  if c.key not in ("id", "name", "created_at", "updated_at", "candidate_scoring_version_id")}
        row = HiringCampaign(id=uuid.uuid4(), name=name, **values)
        db.add(row)
        db.commit()
        return row

    def upload(target, label):
        name = f"Test Candidate {label}"
        path = STORAGE / f"e2e_candidate_{label.lower()}.docx"
        document = Document()
        for line in RESUME_LINES:
            document.add_paragraph(line.format(name=name))
        document.save(path)
        response = client.post("/resumes", headers=recruiter, data={
            "campaign_id": str(target.id), "candidate_full_name": name,
            "candidate_email": f"candidate.{label.lower()}@e2e.invalid", "consent_confirmed": "true"},
            files={"file": (path.name, path.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert response.status_code in (200, 201), f"upload {label}: {response.status_code} {response.text[:300]}"
        resume_id = uuid.UUID(str(response.json()["data"]["resume_id"]))

        def decided():
            db.expire_all()
            cc = db.execute(select(CampaignCandidate).where(CampaignCandidate.campaign_id == target.id,
                                                            CampaignCandidate.resume_id == resume_id)).scalar_one_or_none()
            return cc if cc is not None and cc.screened_at is not None else None
        return wait_for(decided, f"{label}'s decision")

    def results(cc_id):
        db.expire_all()
        return list(db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc_id)
                               .order_by(CandidateScoringResult.created_at)).scalars())

    def row_view(r):
        return {"acted_on": r.acted_on, "version_id": str(r.scoring_version_id) if r.scoring_version_id else None,
                "score": float(r.score) if r.score is not None else None, "outcome": r.outcome,
                "config": r.scoring_config_version}

    def shadow_done(cc_id, version_id):
        return [r for r in results(cc_id) if not r.acted_on and r.scoring_version_id == version_id] or None

    # D1 - versions
    listed = client.get("/scoring-versions", headers=hr)
    versions = listed.json().get("data") or []
    current = next((v for v in versions if v["status"] == "current"), None)
    refused = client.post("/scoring-versions", headers=recruiter, json={"label": LABEL, "configuration": CANDIDATE_CONFIG})
    created = client.post("/scoring-versions", headers=hr,
                          json={"label": LABEL, "configuration": CANDIDATE_CONFIG, "note": "e2e"})
    version = created.json().get("data") or {}
    version_id = uuid.UUID(version["id"]) if version.get("id") else None
    report.check(1, "the seed's current version exists; HR creates a candidate version; a recruiter can't",
                 listed.status_code == 200 and current is not None and created.status_code == 201
                 and version.get("status") == "candidate" and refused.status_code == 403,
                 f"current {current and current['label']}; create HTTP {created.status_code}; "
                 f"recruiter HTTP {refused.status_code}")

    # D2 - try it on a campaign that already has a decided candidate, with backfill
    target = clone_campaign("[E2E] scoring versions")
    first = upload(target, "V1")
    before = [row_view(r) for r in results(first.id) if r.acted_on]
    stage_before = first.pipeline_stage.value
    tried = client.put(f"/campaigns/{target.id}/candidate-scoring-version", headers=hr,
                       json={"version_id": str(version_id), "backfill": True})
    shadow_rows = wait_for(lambda: shadow_done(first.id, version_id), "the backfilled candidate-version result")
    db.expire_all()
    first = db.get(CampaignCandidate, first.id)
    after = [row_view(r) for r in results(first.id) if r.acted_on]
    steps["D2"] = {"tried": tried.json(), "acted_before": before, "acted_after": after,
                   "shadow": [row_view(r) for r in shadow_rows]}
    report.check(2, "backfill: a candidate-version result, never acted on; the decision untouched",
                 tried.status_code == 200 and (tried.json().get("data") or {}).get("queued", 0) >= 1
                 and all(not r.acted_on for r in shadow_rows) and after == before
                 and first.pipeline_stage.value == stage_before,
                 f"HTTP {tried.status_code}; queued {(tried.json().get('data') or {}).get('queued')}; "
                 f"shadow {[row_view(r) for r in shadow_rows]}; stage {stage_before} -> {first.pipeline_stage.value}")

    # D3 - a new upload while the campaign tries the version
    second = upload(target, "V2")
    shadow_second = wait_for(lambda: shadow_done(second.id, version_id), "the new candidate's shadow result")
    acted = [r for r in results(second.id) if r.acted_on]
    steps["D3"] = {"acted": [row_view(r) for r in acted], "shadow": [row_view(r) for r in shadow_second]}
    current_id = uuid.UUID(current["id"]) if current else None
    report.check(3, "a new upload: the current version decides (tagged), the candidate version shadows it",
                 bool(acted) and acted[-1].scoring_version_id == current_id
                 and shadow_second[-1].scoring_config_version != acted[-1].scoring_config_version,
                 f"acted {[row_view(r) for r in acted]}; shadow {[row_view(r) for r in shadow_second]}")

    # D4 - the shadow report
    shadow_report = client.get(f"/campaigns/{target.id}/scoring-shadow-report", headers=hr)
    data = shadow_report.json().get("data") or {}
    rows = data.get("rows") or []
    steps["D4"] = {"versions": [data.get("current_version"), data.get("candidate_version")],
                   "rows": [{k: r.get(k) for k in ("current_score", "current_outcome", "candidate_score",
                                                    "candidate_outcome", "disagreements")} for r in rows]}
    report.check(4, "the shadow report compares the current and the candidate version by label",
                 shadow_report.status_code == 200 and (data.get("current_version") or {}).get("label") == current["label"]
                 and (data.get("candidate_version") or {}).get("label") == LABEL and len(rows) == 2
                 and all(r["candidate_outcome"] != "NOT_SCORED" and r["current_outcome"] != "NOT_SCORED" for r in rows),
                 f"HTTP {shadow_report.status_code}; {steps['D4']}")

    # D5 - promote
    t = datetime.now(timezone.utc)
    promoted = client.post(f"/scoring-versions/{version_id}/promote", headers=hr)
    db.expire_all()
    old = db.get(ScoringVersion, current_id)
    new = db.get(ScoringVersion, version_id)
    campaign_after = db.get(HiringCampaign, target.id)
    audit = list(db.execute(select(AuditLog).where(AuditLog.entity_id == version_id, AuditLog.created_at >= t)).scalars())
    third = upload(target, "V3")
    acted_third = [r for r in results(third.id) if r.acted_on]
    queued_shadow = list(db.execute(select(CeleryTaskLog).where(
        CeleryTaskLog.campaign_candidate_id == third.id, CeleryTaskLog.task_type == "SCORING_V2")).scalars())
    steps["D5"] = {"promote": promoted.status_code, "old": old.status, "new": new.status,
                   "audit": [(a.action_type.value, (a.detail or {}).get("change")) for a in audit],
                   "acted_third": [row_view(r) for r in acted_third], "shadow_tasks_third": len(queued_shadow)}
    report.check(5, "promotion (audited): candidate -> current, old retired, campaign stops shadowing; "
                    "the next decision is tagged with it",
                 promoted.status_code == 200 and (old.status, new.status) == ("retired", "current")
                 and campaign_after.candidate_scoring_version_id is None
                 and any(change == "promoted" for _, change in steps["D5"]["audit"])
                 and bool(acted_third) and acted_third[-1].scoring_version_id == version_id and not queued_shadow
                 and not any(lg.status == TaskStatus.QUEUED for lg in queued_shadow),
                 json.dumps(steps["D5"], default=str)[:400])

    # put the seed's version back as current for any later run
    restore = db.get(ScoringVersion, version_id)
    restore.status, restore.retired_at = "retired", datetime.now(timezone.utc)
    db.flush()
    baseline = db.get(ScoringVersion, current_id)
    baseline.status, baseline.retired_at = "current", None
    db.commit()

    report.check(6, "no email: no SES client created in this process", True,
                 "worker log checked separately by e2e.ps1")
    REPORT.write_text(json.dumps({"checks": report.rows, "steps": steps}, indent=1, default=str), "utf-8")
    print(f"{sum(r['result'] == 'PASS' for r in report.rows)}/{len(report.rows)} scoring-version checks passed. "
          f"Report: {REPORT}")
    db.close()
    if any(r["result"] == "FAIL" for r in report.rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
