"""
Phase 2 end-to-end check (JD calibration, unknown skills, provisional scoring) -
ISOLATED e2e stack only (e2e/README.md). Run after setup_e2e (and, in e2e.ps1
"test", after run_reparse_e2e, which uploads the fixture candidates):

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_phase2_e2e

Drives the real API routes in-process (FastAPI TestClient) as the e2e HR admin
and recruiter; the stack's own Celery worker scores. Uses the Phase 2 JD copy
setup_e2e seeds from the fixture's "phase2" section: not calibrated, with an
unknown CORE skill (Agentic Workflow Design) and an unknown SUPPORTING skill
(Speech Analytics - in candidate K's experience). Any attempt to create an SES
client in this process fails the run.

Checks (PASS / FAIL, report at /data/storage/e2e_phase2_report.json):
  P1  creating a campaign on the uncalibrated JD is refused (409 JD_NOT_CALIBRATED)
  P2  calibration is refused while the core unknown skill is unresolved (422)
  P3  the core unknown skill is mapped (top-3 suggestions offered), the supporting one left
      unresolved; calibration succeeds; the campaign is created
  P4  candidate K (text has the supporting skill) scored: provisional credit 0.8 of its tier
      weight, flagged provisional, outside every gate - v2, the stored decision (v1 retired, phase C)
  P5  resolving the supporting skill marks K "score out of date" and saves a calibration version
  P6  scoring used the calibrated minimum experience; the JD's extraction is unchanged
  P7  no email: no SES client created; the worker logged none (checked by e2e.ps1)
"""
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.run_reparse_e2e import EmailAttempted, Report, _token_payload, wait_for
from scripts.e2e.setup_e2e import FIXTURES, assert_isolated, phase2_jd_title

REPORT = Path("/data/storage/e2e_phase2_report.json")
CAMPAIGN_NAME = "[E2E] Phase 2 - calibrated JD"
PROVISIONAL_CREDIT = 0.8


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
    from app.enums.constants import API_PREFIX
    from app.main import app
    from app.models.jd.job_descriptions import JDCalibrationVersion, JobDescription
    from app.models.pipeline import CampaignCandidate
    from app.models.prompt_template import PromptTemplate
    from app.models.scoring_v2 import CandidateScoringResult
    from app.models.skills import SkillOntology

    report = Report()
    client = TestClient(app, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    hr = {"Authorization": "Bearer e2e:e2e-hr-admin"}
    recruiter = {"Authorization": "Bearer e2e:e2e-recruiter"}
    db = SessionLocal()
    fixture = json.loads((FIXTURES / "jd_ai_transformation_lead.json").read_text("utf-8"))
    phase2 = fixture["phase2"]
    core_unknown = next(u for u in phase2["unknown_skills"] if u["tier"] == "CORE")
    supporting_unknown = next(u for u in phase2["unknown_skills"] if u["tier"] == "SUPPORTING")
    manifest = json.loads((FIXTURES / "candidates.json").read_text("utf-8"))

    jd = db.execute(select(JobDescription).where(JobDescription.title == phase2_jd_title(fixture))).scalar_one()
    extraction_before = json.loads(json.dumps(jd.extracted_json))
    min_before = float(jd.min_experience_years) if jd.min_experience_years is not None else None
    resume_prompt = db.query(PromptTemplate).filter(PromptTemplate.task_type == "RESUME_PARSE").first()
    ai_prompt = db.query(PromptTemplate).filter(PromptTemplate.task_type == "AI_EVALUATE").first()
    settings = fixture["campaign"]
    campaign_payload = {
        "name": CAMPAIGN_NAME, "jd_id": str(jd.id),
        "weight_deterministic": settings["weight_deterministic"], "weight_semantic": settings["weight_semantic"],
        "weight_ai": settings["weight_ai"], "semantic_threshold": settings["semantic_threshold"],
        "ai_threshold": settings["ai_threshold"], "deterministic_threshold": settings["deterministic_threshold"],
        "hiring_manager_id": "e2e-hiring-manager", "recruiter_id": "e2e-recruiter",
        "prompt_template_id": str(resume_prompt.id), "ai_evaluate_prompt_id": str(ai_prompt.id),
    }

    def calibration():
        response = client.get(f"/job-descriptions/{jd.id}/calibration", headers=hr)
        assert response.status_code == 200, response.text[:300]
        return response.json()["data"]

    def calibration_payload(view):
        return {"skills": [{"jd_skill_id": s["jd_skill_id"], "tier": s["tier"] or "SUPPORTING",
                            "sense_key": s.get("sense_key") or s.get("suggested_sense_key")} for s in view["skills"]],
                "min_experience_years": phase2["calibrated_min_experience_years"],
                "experience_tolerance_years": None, "education_requirement": view["education_requirement"]}

    def unknown_id(raw_text):
        view = calibration()
        return next(u["unknown_skill_id"] for u in view["unknown_skills"] if u["raw_text"] == raw_text)

    # P1 - campaign on the uncalibrated JD
    response = client.post("/campaigns", json=campaign_payload, headers=hr)
    body = response.json()
    report.check(1, "creating a campaign on the uncalibrated JD is refused",
                 response.status_code == 409 and (body.get("data") or {}).get("code") == "JD_NOT_CALIBRATED",
                 f"HTTP {response.status_code}, code {(body.get('data') or {}).get('code')}")

    # P2 - calibration refused while the core unknown skill is unresolved
    view = calibration()
    response = client.put(f"/job-descriptions/{jd.id}/calibration", json=calibration_payload(view), headers=hr)
    report.check(2, "calibration refused while the core unknown skill is unresolved",
                 view["blocking_unknown_skills"] == [core_unknown["raw_text"]] and response.status_code == 422
                 and core_unknown["raw_text"] in response.text,
                 f"blocking {view['blocking_unknown_skills']}, HTTP {response.status_code}")

    # P3 - map the core one, leave the supporting one, calibrate, create the campaign
    suggestions = client.get(f"/job-descriptions/{jd.id}/unknown-skills/suggestions", headers=hr).json()["data"]
    target = db.execute(select(SkillOntology).where(SkillOntology.canonical_name == core_unknown["map_to"])).scalar_one()
    mapped = client.post(f"/job-descriptions/{jd.id}/unknown-skills/{unknown_id(core_unknown['raw_text'])}/resolve",
                         json={"action": "MAP", "canonical_skill_id": str(target.id)}, headers=hr)
    view = calibration()
    pending = [u["raw_text"] for u in view["unknown_skills"] if u["status"] == "PENDING"]
    mapped_row = next((s for s in view["skills"] if s["name"] == core_unknown["map_to"]), None)
    calibrated = client.put(f"/job-descriptions/{jd.id}/calibration", json=calibration_payload(view), headers=hr)
    created = client.post("/campaigns", json=campaign_payload, headers=hr)
    campaign_id = (created.json().get("data") or {}).get("id")
    report.check(3, "core unknown skill mapped (supporting left), JD calibrated, campaign created",
                 all(len(s["suggestions"]) <= 3 for s in suggestions) and mapped.status_code == 200
                 and mapped_row is not None and mapped_row["tier"] == "CORE"
                 and pending == [supporting_unknown["raw_text"]]
                 and calibrated.status_code == 200 and calibrated.json()["data"]["calibration_version"] == 1
                 and created.status_code == 201 and campaign_id is not None,
                 f"suggestion lists {[len(s['suggestions']) for s in suggestions]}; map HTTP {mapped.status_code}; "
                 f"{core_unknown['map_to']} tier {mapped_row and mapped_row['tier']}; still pending {pending}; "
                 f"calibrate HTTP {calibrated.status_code}; create HTTP {created.status_code}")

    # P4 - K scored with provisional credit for the supporting skill (v2 decides; v1 no longer runs)
    campaign_id = uuid.UUID(campaign_id)
    info = manifest["K"]
    upload = client.post("/resumes", headers=recruiter, data={
        "campaign_id": str(campaign_id), "candidate_full_name": info["full_name"], "candidate_email": info["email"],
        "consent_confirmed": "true", "resolution": "use_existing"}, files={
        "file": (info["file"], (FIXTURES / info["file"]).read_bytes(),
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert upload.status_code in (200, 201), f"upload K: {upload.status_code} {upload.text[:300]}"
    k_resume_id = uuid.UUID(str(upload.json()["data"]["resume_id"]))

    def k_cc():
        db.expire_all()
        cc = db.execute(select(CampaignCandidate).where(CampaignCandidate.campaign_id == campaign_id,
                                                        CampaignCandidate.resume_id == k_resume_id)).scalar_one_or_none()
        return cc if cc is not None and cc.deterministic_breakdown else None

    def k_v2():
        db.expire_all()
        cc = k_cc()
        if cc is None:
            return None
        return db.execute(select(CandidateScoringResult).where(
            CandidateScoringResult.campaign_candidate_id == cc.id, CandidateScoringResult.scoring_version == "v2")
            .order_by(CandidateScoringResult.created_at.desc())).scalars().first()

    cc = wait_for(k_cc, "K's score in the Phase 2 campaign")
    v2 = wait_for(k_v2, "K's v2 result")
    stored = cc.deterministic_breakdown                 # v2's own breakdown since phase C
    v2_prov = next((p for p in v2.breakdown.get("provisional_skills") or []
                    if p["canonical_name"] == supporting_unknown["raw_text"]), {})
    stored_prov = next((p for p in stored.get("provisional_skills") or []
                        if p["canonical_name"] == supporting_unknown["raw_text"]), {})
    v2_names = {e["canonical_name"] for e in v2.breakdown["mandatory_skills"]}
    db.expire_all()
    jd_core = sum(1 for s in calibration()["skills"] if s["tier"] == "CORE")
    report.check(4, "K gets provisional credit 0.8 for the supporting skill, flagged provisional, outside every gate",
                 v2.acted_on is True
                 and stored.get("scoring_version") == "v2" and "v2" not in stored and stored_prov == v2_prov
                 and v2_prov.get("provisional") is True and v2_prov.get("quality") == PROVISIONAL_CREDIT
                 and v2_prov.get("counts_for_core_gate") is False and supporting_unknown["raw_text"] not in v2_names
                 and v2.breakdown["core_skill_count"] == jd_core,
                 f"stored breakdown {stored.get('scoring_version')} (acted on {v2.acted_on}); "
                 f"v2 {v2_prov.get('match_type')} quality {v2_prov.get('quality')}, gate {v2_prov.get('counts_for_core_gate')}; "
                 f"v2 core count {v2.breakdown['core_skill_count']} = JD core {jd_core}")

    # P5 - resolve the supporting skill: K out of date, calibration version saved
    resolved = client.post(
        f"/job-descriptions/{jd.id}/unknown-skills/{unknown_id(supporting_unknown['raw_text'])}/resolve",
        json={"action": "NEW"}, headers=hr)
    db.expire_all()
    cc_after = db.get(CampaignCandidate, cc.id)
    versions = db.execute(select(JDCalibrationVersion).where(JDCalibrationVersion.jd_id == jd.id)
                          .order_by(JDCalibrationVersion.version_no)).scalars().all()
    latest = versions[-1] if versions else None
    note = ((latest.changes or {}).get("note") if latest else "") or ""
    report.check(5, "resolving the supporting skill marks K out of date and saves a calibration version",
                 resolved.status_code == 200 and bool(cc_after.score_out_of_date_reason)
                 and [v.version_no for v in versions] == [1, 2] and supporting_unknown["raw_text"] in note,
                 f"resolve HTTP {resolved.status_code}; out of date: {bool(cc_after.score_out_of_date_reason)}; "
                 f"versions {[v.version_no for v in versions]}; note '{note[:80]}'")

    # P6 - calibrated minimum used; extraction unchanged
    db.expire_all()
    jd_after = db.get(JobDescription, jd.id)
    experience = stored.get("experience_validation") or {}
    v2_min = (v2.breakdown.get("checks") and next((c for c in v2.breakdown["checks"] if c["check"] == "Experience"), {})) or {}
    calibrated_min = float(phase2["calibrated_min_experience_years"])
    report.check(6, "scoring used the calibrated minimum; the extraction is unchanged",
                 experience.get("min_years") == calibrated_min and f"minimum {calibrated_min} " in (v2_min.get("detail") or "")
                 and jd_after.extracted_json == extraction_before
                 and (float(jd_after.min_experience_years) if jd_after.min_experience_years is not None else None) == min_before
                 and float(jd_after.calibrated_min_experience_years) == calibrated_min,
                 f"stored min_years {experience.get('min_years')}; v2 '{v2_min.get('detail')}'; extraction unchanged "
                 f"{jd_after.extracted_json == extraction_before}; column {jd_after.min_experience_years} (was {min_before})")

    # P7 - no SES client in this process (the boto3 guard would have raised)
    report.check(7, "no email: no SES client created in this process", True,
                 "worker log checked separately by e2e.ps1")

    REPORT.write_text(json.dumps({"checks": report.rows}, indent=1), "utf-8")
    passed = sum(1 for r in report.rows if r["result"] == "PASS")
    print(f"\n{passed}/{len(report.rows)} Phase 2 checks passed. Report: {REPORT}")
    db.close()
    if passed != len(report.rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
