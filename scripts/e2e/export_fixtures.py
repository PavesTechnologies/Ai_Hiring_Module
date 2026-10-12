"""
One-off, READ-ONLY export of the fixtures the isolated e2e stack loads
(tests/fixtures/e2e/). Run against the live database only to refresh them;
the e2e stack itself never connects to it.

    python -m scripts.e2e.export_fixtures --campaign "<campaign>" --resume-v "<file name part>" --resume-k "<file name part>"

Writes:
  jd_ai_transformation_lead.json   the campaign's JD + its jd_skills (by skill name)
  skill_catalogue.json             active skill_ontology rows (names/aliases/parents, no embeddings)
  candidate_v.docx / candidate_k.docx + candidates.json
                                   two resumes, anonymised: candidate name, employers, institutions,
                                   contact lines and URLs replaced; PII redacted as the pipeline does.
Prints counts and leak checks only - never resume content.
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

logging.disable(logging.WARNING)

from app.db.session import SessionLocal, engine  # noqa: E402

engine.echo = False
import app.models  # noqa: E402,F401
from app.models.campaigns import HiringCampaign  # noqa: E402
from app.models.candidates import Resume  # noqa: E402
from app.models.pipeline import CampaignCandidate  # noqa: E402
from app.models.skills import JDSkill, SkillOntology  # noqa: E402
from app.services.skills.skill_evidence_service import redacted_resume_text  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "e2e"
# label -> substring of the stored file name (matched case-insensitively)
_CONTACT = re.compile(r"\[(EMAIL|PHONE|LINKEDIN|URL|ADDRESS)\]|https?://\S+|www\.\S+|\S+@\S+", re.I)
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")


def _letter(i: int) -> str:
    return chr(ord("A") + i)


def anonymise(text: str, parsed: dict, label: str) -> tuple[str, dict]:
    replacements: list[tuple[str, str]] = []
    full_name = (parsed or {}).get("full_name") or ""
    for part in sorted({p for p in re.split(r"\s+", full_name) if len(p) >= 3}, key=len, reverse=True):
        replacements.append((part, "Candidate" if part == full_name.split()[0] else label))
    companies = []
    for entry in (parsed or {}).get("work_experience") or []:
        company = (entry.get("company") or "").strip()
        if company and company not in companies:
            companies.append(company)
    for i, company in enumerate(companies):
        replacements.append((company, f"Employer {_letter(i)}"))
    institutions = []
    for entry in (parsed or {}).get("education") or []:
        name = (entry.get("institution") or "").strip()
        if name and name not in institutions:
            institutions.append(name)
    for i, name in enumerate(institutions):
        replacements.append((name, f"University {_letter(i)}"))

    counts = {"name": 0, "employer": 0, "institution": 0, "contact_lines_dropped": 0}
    lines = []
    for line in (text or "").splitlines():
        phone = any(sum(ch.isdigit() for ch in m.group(0)) >= 10 for m in _PHONE.finditer(line))
        if _CONTACT.search(line) or phone:
            counts["contact_lines_dropped"] += 1
            continue
        for old, new in sorted(replacements, key=lambda r: len(r[0]), reverse=True):
            pattern = re.compile(re.escape(old), re.I)
            hits = len(pattern.findall(line))
            if hits:
                key = "employer" if new.startswith("Employer") else "institution" if new.startswith("University") else "name"
                counts[key] += hits
                line = pattern.sub(new, line)
        lines.append(line)
    out = "\n".join(lines)
    leaks = [old for old, _ in replacements if re.search(re.escape(old), out, re.I)]
    return out, {**counts, "residual_leaks": len(leaks)}


def text_from_parse(parsed: dict) -> str:
    """Resume-shaped text rebuilt from a stored parse - used when the stored file can't be downloaded."""
    p = parsed or {}
    lines = [p.get("full_name") or "", "PROFILE", p.get("summary") or ""]
    if p.get("skills"):
        lines += ["Core Competencies", " | ".join(p["skills"])]
    lines.append("PROFESSIONAL EXPERIENCE")
    for job in p.get("work_experience") or []:
        lines.append(" - ".join(str(v) for v in (job.get("title"), job.get("company")) if v))
        dates = " - ".join(str(v) for v in (job.get("start_date"), job.get("end_date")) if v)
        if dates:
            lines.append(dates)
        for item in (job.get("responsibilities") or job.get("achievements") or []):
            lines.append(str(item))
        if job.get("description"):
            lines.append(str(job["description"]))
    if p.get("education"):
        lines.append("EDUCATION")
        for edu in p["education"]:
            lines.append(", ".join(str(v) for v in (edu.get("degree"), edu.get("field_of_study"), edu.get("institution"),
                                                     edu.get("end_date") or edu.get("year")) if v))
    if p.get("certifications"):
        lines.append("CERTIFICATIONS")
        for cert in p["certifications"]:
            lines.append(cert.get("name") if isinstance(cert, dict) else str(cert))
    return "\n".join(line for line in lines if line is not None)


def write_docx(text: str, path: Path) -> None:
    from docx import Document
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    # Which two resumes to export: a part of each upload's file name, given at
    # run time (never hard-coded - they are real candidates).
    parser.add_argument("--resume-v", required=True, help="part of fixture V's resume file name")
    parser.add_argument("--resume-k", required=True, help="part of fixture K's resume file name")
    args = parser.parse_args()
    candidates = {"V": args.resume_v, "K": args.resume_k}
    OUT.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        campaign = db.query(HiringCampaign).filter(HiringCampaign.name.ilike(f"%{args.campaign}%")).first()
        if campaign is None:
            raise SystemExit("campaign not found")
        jd = campaign.job_description
        names = dict(db.query(SkillOntology.id, SkillOntology.canonical_name).all())
        jd_skills = []
        for row in db.query(JDSkill).filter(JDSkill.jd_id == jd.id).all():
            jd_skills.append({
                "skill": names.get(row.canonical_skill_id), "mandatory": row.mandatory,
                "weight": float(row.weight) if row.weight is not None else None,
                "importance": getattr(row.importance, "value", row.importance),
                "aliases": [names.get(a) for a in (row.alias_skill_ids or []) if names.get(a)],
                "match_tier": row.match_tier, "confidence": row.confidence,
                "verification_status": getattr(row.verification_status, "value", row.verification_status),
            })
        jd_fixture = {
            "campaign": {k: (float(v) if isinstance(v, (int, float)) or hasattr(v, "as_integer_ratio") else v)
                         for k, v in {
                             "name": campaign.name, "weight_deterministic": campaign.weight_deterministic,
                             "weight_semantic": campaign.weight_semantic, "weight_ai": campaign.weight_ai,
                             "semantic_threshold": campaign.semantic_threshold, "ai_threshold": campaign.ai_threshold,
                             "deterministic_threshold": campaign.deterministic_threshold,
                             "required_skill_coverage_threshold": campaign.required_skill_coverage_threshold,
                             "max_missing_core_skills": campaign.max_missing_core_skills,
                             "max_missing_core_pct": campaign.max_missing_core_pct,
                         }.items()},
            "jd": {
                "title": jd.title, "raw_text": jd.raw_text, "extracted_json": jd.extracted_json,
                "required_skills": jd.required_skills,
                "min_experience_years": float(jd.min_experience_years) if jd.min_experience_years is not None else None,
                "max_experience_years": float(jd.max_experience_years) if jd.max_experience_years is not None else None,
                "notice_period": jd.notice_period, "education_criteria": jd.education_criteria,
                "source_format": jd.source_format.value, "jurisdiction": jd.jurisdiction,
            },
            "jd_skills": jd_skills,
        }
        (OUT / "jd_ai_transformation_lead.json").write_text(json.dumps(jd_fixture, indent=1, default=str), "utf-8")

        parents = {s.id: s.canonical_name for s in db.query(SkillOntology).all()}
        catalogue = [
            {"canonical_name": s.canonical_name, "aliases": list(s.aliases or []), "category": s.category,
             "parent": parents.get(s.parent_skill_id), "confidence": s.confidence, "source": s.source}
            for s in db.query(SkillOntology).filter(SkillOntology.is_active.is_(True)).all()
        ]
        (OUT / "skill_catalogue.json").write_text(json.dumps(catalogue, indent=1), "utf-8")
        print(f"JD fixture: {len(jd_skills)} jd_skills; catalogue: {len(catalogue)} skills")

        manifest = {}
        pairs = db.query(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id) \
            .filter(CampaignCandidate.campaign_id == campaign.id).all()
        for label, needle in candidates.items():
            resume = next((r for _, r in pairs if needle.lower() in (r.original_filename or "").lower()), None)
            if resume is None:
                print(f"candidate {label}: not found")
                continue
            try:
                source, raw_text = "stored file", redacted_resume_text(resume)
            except Exception as exc:
                source, raw_text = f"stored parse (file unavailable: {type(exc).__name__})", text_from_parse(resume.parsed_json)
            text, report = anonymise(raw_text, resume.parsed_json or {}, label)
            write_docx(text, OUT / f"candidate_{label.lower()}.docx")
            manifest[label] = {"file": f"candidate_{label.lower()}.docx", "full_name": f"Test Candidate {label}",
                               "email": f"candidate.{label.lower()}@example.invalid", "lines": len(text.splitlines()),
                               "source": source}
            print(f"candidate {label}: source={source} {report}")
        (OUT / "candidates.json").write_text(json.dumps(manifest, indent=1), "utf-8")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
