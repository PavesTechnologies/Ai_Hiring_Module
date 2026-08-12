"""
One-time backfill for the 8 [SEED] M12 Backend Engineer Test Campaign
resumes created before seed_m12_test_campaign.py populated parsed_json/
file_size_bytes/page_count - those rows were left with parse_status=PARSED
but parsed_json=NULL, which is internally inconsistent (found via a real API
response comparison: a genuinely parsed resume always has parsed_json
content) and looked like a parse failure to whatever downstream check
flagged it.

Idempotent: only updates rows where parsed_json IS NULL, scoped to resumes
belonging to campaign_candidates under the tagged seed campaign - never
touches the 2 real campaigns or their resumes.

Safe to delete once seed_m12_test_campaign.py (which now sets these fields
on creation) has been used for every future (re)seed of this campaign.
"""
import hashlib

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.campaigns import HiringCampaign
from app.models.candidates import Resume
from app.models.pipeline import CampaignCandidate

SEED_CAMPAIGN_NAME = "[SEED] M12 Backend Engineer Test Campaign"
REQUIRED_SKILLS = ["Python", "FastAPI", "PostgreSQL", "SQLAlchemy", "REST API", "git", "Docker", "Microservices", "pytest"]
PREFERRED_SKILLS = ["Django", "Kubernetes", "AWS", "Redis", "Celery"]
TOTAL_EXPERIENCE_YEARS = [2.0, 3.0, 5.5, 3.5, 4.0, 4.5, 5.0, 5.5]


def _parsed_json_for_candidate(i: int, total_experience_years: float) -> dict:
    all_skills = [s.lower() for s in REQUIRED_SKILLS + PREFERRED_SKILLS]
    return {
        "full_name": f"[SEED] Candidate {i}",
        "skills": all_skills,
        "soft_skills": [],
        "work_experience": [{
            "title": "Backend Engineer",
            "company": "[SEED] Data Co.",
            "start_date": "January 2022",
            "end_date": "Present",
            "is_current": True,
            "is_internship": False,
            "is_volunteer": False,
            "description": (
                f"Developed backend services using {', '.join(all_skills[:5])} "
                "and related technologies."
            ),
        }],
        "education": [{
            "degree": "Bachelor of Technology (B.Tech)",
            "institution": None,
            "field": "computer science and engineering",
            "graduation_year": None,
        }],
        "projects": [{
            "name": "[SEED] Backend Platform",
            "description": "Placeholder seed project - not a real candidate submission.",
            "tech": all_skills[:6],
        }],
        "certifications": [],
        "total_experience_years": total_experience_years,
        "department": None,
        "location": None,
        "summary": (
            f"Backend developer with {total_experience_years} years of experience "
            f"using {', '.join(all_skills[:5])}. Placeholder seed summary."
        ),
        "metadata": {},
    }


db = SessionLocal()

try:
    campaign = db.query(HiringCampaign).filter(HiringCampaign.name == SEED_CAMPAIGN_NAME).first()
    if campaign is None:
        print(f"Seed campaign '{SEED_CAMPAIGN_NAME}' not found — nothing to backfill.")
    else:
        resumes = db.execute(
            select(Resume)
            .join(CampaignCandidate, CampaignCandidate.resume_id == Resume.id)
            .where(CampaignCandidate.campaign_id == campaign.id, Resume.parsed_json.is_(None))
            .order_by(Resume.original_filename)
        ).scalars().all()

        if not resumes:
            print("No seed resumes with NULL parsed_json found — already backfilled.")
        else:
            for resume in resumes:
                # original_filename is "seed_candidate_{i}_resume.pdf"
                i = int(resume.original_filename.split("_")[2])
                total_experience_years = TOTAL_EXPERIENCE_YEARS[i - 1]
                resume.parsed_json = _parsed_json_for_candidate(i, total_experience_years)
                resume.file_size_bytes = 2800 + (i * 37)
                resume.page_count = 1
                resume.file_hash = hashlib.sha256(f"seed-m12-resume-{i}".encode("utf-8")).hexdigest()
                print(f"Backfilled parsed_json for resume {resume.id} (candidate {i})")

            db.commit()
            print(f"\nBackfilled {len(resumes)} resume(s).")

except Exception as e:
    db.rollback()
    print(f"Error backfilling M12 resume parsed_json: {e}")
    raise

finally:
    db.close()
