"""
One-off converter: freezes the synthetic candidates scored in dev into the golden set - their JDs (row,
calibration, JD skills, unknown skills, campaign settings) and their resumes (redacted text, parse,
matched skills, resume row) - and builds the cases from the fixtures' expectations.json files.
No LLM call. Reads dev only.

    python -m scripts.golden.freeze_from_dev            show what would be written, and any fixture drift
    python -m scripts.golden.freeze_from_dev --write    write tests/golden/

Fixture drift: each frozen text is compared with what the fixture .docx gives locally (text extraction,
cleaning and PII redaction - the parse's own first stages). A difference means the fixture and the
parse dev holds are not the same resume; refreeze that case from the fixture.
"""
import argparse
import datetime
import decimal
import enum
import hashlib
import json
import sys
import uuid
from pathlib import Path

from scripts.golden.golden_format import GOLDEN_DIR, ROOT, SCHEMA_VERSION, validate

CAMPAIGNS = {
    "[P3] Junior Python Developer": "JD-F",
    "[P3] ServiceNow ITSM": "JD-D",
    "[SENIOR] Head of Data & AI Platforms": "JD-S",
}
FIXTURE_SETS = (("phase3", "phase3"), ("hard_negatives", "hard_negative"), ("senior", "senior"))
# Dev's F1 is the re-uploaded F1_v2 (F1 plus one line, from the duplicate-upload fixtures).
SOURCE_OVERRIDES = {"F1": "tests/fixtures/duplicates/F1_v2.docx"}
# S6: HOLD or REJECT, both accepted (decided 2026-10-26).
ACCEPTED_OUTCOMES = {"S6": ["HOLD", "REJECT"]}
# Never frozen: identities, links to users / other rows, timestamps, storage paths, hashes.
SKIP_COLUMNS = {"id", "created_at", "updated_at", "created_by", "updated_by", "calibrated_by", "calibrated_at",
                "parent_jd_id", "lineage_root_id", "calibration_draft_from", "file_path", "content_hash",
                "jd_id", "candidate_id", "resume_id", "org_id", "hiring_manager_id", "recruiter_id",
                "verified_by", "verified_at", "canonical_skill_id", "alias_skill_ids", "prompt_template_id",
                "ai_evaluate_prompt_id", "parse_text_hashes", "file_hash", "uploaded_by", "deleted_at",
                "activated_at", "closed_at", "paused_at", "archived_at", "bulk_upload_job_id", "task_id",
                "unknown_skill_id", "job_id"}


def plain(value):
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return None
    return value


def columns(row, skip=SKIP_COLUMNS) -> dict:
    return {c.key: plain(getattr(row, c.key)) for c in row.__table__.columns
            if c.key not in skip and not isinstance(getattr(row, c.key), bytes)}


def local_redacted_text(docx: Path) -> str:
    """The fixture's text as the parse's first stages make it: extraction, cleaning, PII redaction."""
    from app.models.resume.resume_source_format import ResumeSourceFormat
    from app.services.ai.preprocessing_service import PreprocessingService
    from app.services.pii.pii_detection_service import PIIDetectionService
    from app.services.pii.pii_redaction_service import PIIRedactionService
    from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
    text = PreprocessingService().normalize(ResumeTextExtractionService.extract(docx.read_bytes(), ResumeSourceFormat.DOCX))
    return PIIRedactionService().redact(text, PIIDetectionService().detect(text))


def exact_core(text) -> str | None:
    import re
    return text if isinstance(text, str) and re.fullmatch(r"\d+/\d+", text.strip()) else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    import logging
    logging.disable(logging.WARNING)
    import app.models  # noqa: F401
    from sqlalchemy import select
    from app.db.session import SessionLocal, engine
    from app.models.campaigns import HiringCampaign
    from app.models.candidates import Candidate, Resume
    from app.models.pipeline import CampaignCandidate
    from app.models.prompt_template import PromptTemplate
    from app.models.skills import CandidateSkill, JDSkill, JDUnknownSkill, SkillOntology, UnknownSkill
    from app.services.skills.skill_evidence_service import redacted_resume_text

    engine.echo = False
    db = SessionLocal()
    try:
        names = dict(db.execute(select(SkillOntology.id, SkillOntology.canonical_name)).all())
        prompts = dict(db.execute(select(PromptTemplate.id, PromptTemplate.name)).all())
        jds, resumes, by_id = {}, {}, {}
        for campaign_name, key in CAMPAIGNS.items():
            campaign = db.execute(select(HiringCampaign).where(HiringCampaign.name == campaign_name)).scalar_one()
            jd = campaign.job_description
            skills = []
            for row in db.execute(select(JDSkill).where(JDSkill.jd_id == jd.id)).scalars():
                skills.append({"skill": names[row.canonical_skill_id], **columns(row),
                               "aliases": sorted(names[i] for i in (row.alias_skill_ids or []))})
            unknown = [{"raw_text": u.raw_text, **columns(link, SKIP_COLUMNS | {"unknown_skill_id", "resolved_by",
                                                                                "resolved_at"}),
                        "resolved_skill": names.get(link.resolved_canonical_skill_id)}
                       for link, u in db.execute(select(JDUnknownSkill, UnknownSkill)
                                                 .join(UnknownSkill, UnknownSkill.id == JDUnknownSkill.unknown_skill_id)
                                                 .where(JDUnknownSkill.jd_id == jd.id)).all()]
            for item in unknown:
                item.pop("resolved_canonical_skill_id", None)
            jds[key] = {
                "key": key, "campaign_name": campaign_name,
                "job_description": columns(jd), "jd_skills": sorted(skills, key=lambda s: s["skill"]),
                "unknown_skills": sorted(unknown, key=lambda u: u["raw_text"]),
                "campaign": {**columns(campaign), "prompt_template": prompts.get(campaign.prompt_template_id),
                             "ai_evaluate_prompt": prompts.get(campaign.ai_evaluate_prompt_id)},
            }
            rows = db.execute(select(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                              .where(CampaignCandidate.campaign_id == campaign.id)).all()
            for cc, resume in rows:
                rid = Path(resume.original_filename).stem.upper().split("_")[0]
                text = redacted_resume_text(resume)
                matched = [{"skill": names.get(s.canonical_skill_id), **columns(s)}
                           for s in db.execute(select(CandidateSkill).where(CandidateSkill.resume_id == resume.id)).scalars()]
                resumes[rid] = {
                    "id": rid, "jd": key, "original_filename": resume.original_filename,
                    "redacted_text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "parsed_json": resume.parsed_json, "candidate_skills": sorted(matched, key=lambda s: (
                        s["skill"] or "", s.get("raw_extracted_text") or "")),
                    "resume": columns(resume, SKIP_COLUMNS | {"parsed_json", "original_filename"}),
                    "candidate": {"jurisdiction": db.get(Candidate, resume.candidate_id).jurisdiction},
                    "parse": {"source": "dev", "prompt_template": jds[key]["campaign"]["prompt_template"],
                              "frozen_on": datetime.date.today().isoformat()},
                }
                by_id[rid] = key
        cases, drift = [], []
        for folder, set_name in FIXTURE_SETS:
            for e in json.loads((ROOT / "tests" / "fixtures" / folder / "expectations.json").read_text("utf-8")):
                rid = e["id"]
                docx = Path(SOURCE_OVERRIDES.get(rid, f"tests/fixtures/{folder}/{rid}.docx"))
                assert rid in resumes, f"{rid} is not in dev's synthetic campaigns"
                if local_redacted_text(ROOT / docx) != resumes[rid]["redacted_text"]:
                    drift.append(rid)
                resumes[rid]["source_docx"] = docx.as_posix()
                cases.append({
                    "id": rid, "set": set_name, "jd": by_id[rid], "resume": f"resumes/{rid}.json",
                    "source_docx": docx.as_posix(),
                    "expected": {"outcomes": ACCEPTED_OUTCOMES.get(rid, [e["expected_outcome"]]),
                                 "core": exact_core(e.get("expected_core")),
                                 "note": e.get("reason")},
                    **{k: e[k] for k in ("tests", "trap") if e.get(k)},
                })
        assert len(cases) == len(resumes) == 34, (len(cases), len(resumes))
        golden = {"schema_version": SCHEMA_VERSION,
                  "description": "Synthetic golden set: Phase 3 (F1-F8, D1-D8), hard negatives (H1-H10), "
                                 "senior (S1-S8), and counterfactual pairs. See scripts/golden/golden_format.py.",
                  "jds": {key: f"jds/{key}.json" for key in jds}, "cases": cases}
        print(f"{len(jds)} JDs, {len(cases)} cases | fixture drift (frozen text differs from the .docx): "
              f"{drift or 'none'}")
        if not args.write:
            print("dry run - nothing written (use --write)")
            return 0
        (GOLDEN_DIR / "jds").mkdir(parents=True, exist_ok=True)
        (GOLDEN_DIR / "resumes").mkdir(parents=True, exist_ok=True)
        for key, jd in jds.items():
            _dump(GOLDEN_DIR / "jds" / f"{key}.json", jd)
        for rid, resume in resumes.items():
            _dump(GOLDEN_DIR / "resumes" / f"{rid}.json", resume)
        _dump(GOLDEN_DIR / "golden_set.json", golden)
        problems = validate(golden, GOLDEN_DIR)
        print("written; validation:", problems or "ok")
        return 1 if problems else 0
    finally:
        db.rollback()
        db.close()


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
