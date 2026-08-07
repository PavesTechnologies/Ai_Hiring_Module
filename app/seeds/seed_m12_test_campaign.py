"""
M12 (Workflow & Interview Scheduling) isolated test data: a dedicated
[SEED]-tagged JD, campaign, and candidate pool, deliberately separate from
the 2 real hiring_campaigns rows ("Java Dev hiring 2026" /
"senior java developer 2") and their 14 real campaign_candidates - those
have real, currently-growing data from concurrent work elsewhere on this
dev DB, and M12 test data should never touch them.

Idempotent: checks for the tagged campaign by name before doing anything -
if found, the whole batch is skipped (this script does not partially
top-up an existing run).

Candidate PII is encrypted for real via EncryptionService (the same code
path the app uses for genuine uploads), against the real ACTIVE
CANDIDATE_PII encryption key already seeded in this environment - not
fabricated ciphertext.
"""
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.encryption_service import EncryptionService
from app.db.session import SessionLocal
from app.models.candidates import Candidate, FileFormat, ParseStatus, Resume
from app.models.campaigns import HiringCampaign
from app.models.jd.job_descriptions import JobDescription, JDSourceFormat, JDVerificationStatus
from app.models.pipeline import (
    AIRecommendation,
    CampaignCandidate,
    CampaignCandidateStageHistory,
    PipelineStage,
    TransitionSource,
)
from app.models.prompt_template import PromptTemplate
from app.models.skills import JDSkill, JDSkillVerificationStatus, SkillOntology
from app.repositories.encryption_key_repository import EncryptionKeyRepository

SEED_CAMPAIGN_NAME = "[SEED] M12 Backend Engineer Test Campaign"
SEED_JD_TITLE = "[SEED] Backend Engineer (Python)"

ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
HR_ADMIN_ID = "5100005"
HIRING_MANAGER_ID = "5100022"
RECRUITER_ID = "5100024"


def _active_prompt_template_id(db, task_type: str) -> uuid.UUID:
    """
    Looked up live by task_type rather than hardcoded, since prompt_templates
    ids are not stable across environments/resets (found the hard way after
    a dev-DB reset repopulated this table with different real rows/ids).
    """
    row = db.query(PromptTemplate).filter(
        PromptTemplate.task_type == task_type, PromptTemplate.status == "ACTIVE",
    ).first()
    if row is None:
        raise ValueError(f"No ACTIVE prompt_templates row for task_type='{task_type}'.")
    return row.id

REQUIRED_SKILLS = ["Python", "FastAPI", "PostgreSQL", "SQLAlchemy", "REST API", "git", "Docker", "Microservices", "pytest"]
PREFERRED_SKILLS = ["Django", "Kubernetes", "AWS", "Redis", "Celery"]

JD_RAW_TEXT = (
    "Backend Engineer (Python)\n"
    "Job Summary\n"
    "We are seeking a Backend Engineer with strong Python experience to build "
    "scalable services using FastAPI and PostgreSQL.\n"
    "Responsibilities\n"
    "Develop scalable Python services.\n"
    "Design RESTful APIs.\n"
    "Build backend services using FastAPI.\n"
    "Integrate PostgreSQL with SQLAlchemy.\n"
    "Write unit tests using pytest.\n"
    "Containerize services with Docker.\n"
    "Collaborate with frontend teams.\n"
    "Participate in Agile ceremonies.\n"
    "Required Skills\n"
    "Python\nFastAPI\nPostgreSQL\nSQLAlchemy\nREST API\ngit\nDocker\nMicroservices\npytest\n"
    "Preferred Skills\n"
    "Django\nKubernetes\nAWS\nRedis\nCelery\n"
    "Experience\n3-6 years\n"
    "Education\nBachelor's Degree in Computer Science or related field"
)

# (pipeline_stage, composite_score, ai_recommendation, is_fraud_flagged)
CANDIDATE_PLAN = [
    (PipelineStage.UPLOADED, None, None, False),
    (PipelineStage.SHORTLISTED, 58, AIRecommendation.HOLD, False),
    (PipelineStage.SHORTLISTED, 91, AIRecommendation.SHORTLIST, True),
    (PipelineStage.HM_REVIEW, 65, AIRecommendation.HOLD, False),
    (PipelineStage.HM_REVIEW, 74, AIRecommendation.SHORTLIST, False),
    (PipelineStage.HM_REVIEW, 83, AIRecommendation.SHORTLIST, False),
    (PipelineStage.INTERVIEW, 88, AIRecommendation.SHORTLIST, False),
    (PipelineStage.INTERVIEW, 92, AIRecommendation.SHORTLIST, False),
]

# Ordered path every candidate walks through, up to its target stage.
STAGE_PATH = [PipelineStage.UPLOADED, PipelineStage.SCREENING, PipelineStage.SHORTLISTED, PipelineStage.HM_REVIEW, PipelineStage.INTERVIEW]


def _campaign_candidate_idempotency_key(campaign_id, candidate_id, resume_id) -> str:
    raw = f"{campaign_id}:{candidate_id}:{resume_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


db = SessionLocal()

try:
    existing_campaign = db.query(HiringCampaign).filter(HiringCampaign.name == SEED_CAMPAIGN_NAME).first()
    if existing_campaign:
        print(f"Seed campaign '{SEED_CAMPAIGN_NAME}' already exists (id={existing_campaign.id}) — skipping entire batch.")
    else:
        encryption_service = EncryptionService(EncryptionKeyRepository(db))
        jd_parse_prompt_template_id = _active_prompt_template_id(db, "JD_PARSE")
        resume_parse_prompt_template_id = _active_prompt_template_id(db, "RESUME_PARSE")

        # --- 1. Job description -------------------------------------------------
        jd = JobDescription(
            id=uuid.uuid4(),
            org_id=None,
            title=SEED_JD_TITLE,
            raw_text=JD_RAW_TEXT,
            extracted_json={
                "required_skills": [s.lower() for s in REQUIRED_SKILLS],
                "preferred_skills": [s.lower() for s in PREFERRED_SKILLS],
                "experience": {"min_experience_years": 3.0, "max_experience_years": 6.0},
                "education": {"degree": "bachelor's", "field": "computer science or related field"},
            },
            required_skills={"required": [s.lower() for s in REQUIRED_SKILLS], "preferred": [s.lower() for s in PREFERRED_SKILLS]},
            min_experience_years=3.0,
            max_experience_years=6.0,
            notice_period=30,
            education_criteria={"degree": "bachelor's", "field": "computer science or related field"},
            source_format=JDSourceFormat.TEXT,
            content_hash=hashlib.sha256(JD_RAW_TEXT.encode("utf-8")).hexdigest(),
            jurisdiction="INDIA",
            created_by=HR_ADMIN_ID,
            prompt_template_id=jd_parse_prompt_template_id,
            is_verified=JDVerificationStatus.VERIFIED,
        )
        db.add(jd)
        db.flush()
        print(f"Added job_description: {jd.title} (id={jd.id})")

        skill_ids = {
            name: row.id
            for name, row in (
                (r.canonical_name, r)
                for r in db.execute(
                    select(SkillOntology).where(SkillOntology.canonical_name.in_(REQUIRED_SKILLS + PREFERRED_SKILLS))
                ).scalars()
            )
        }
        missing = set(REQUIRED_SKILLS + PREFERRED_SKILLS) - set(skill_ids)
        if missing:
            raise ValueError(f"skill_ontology missing expected canonical names: {missing}")

        for name in REQUIRED_SKILLS:
            db.add(JDSkill(
                id=uuid.uuid4(), jd_id=jd.id, canonical_skill_id=skill_ids[name],
                mandatory=True, weight=1.00, confidence=1.0, match_tier="EXACT",
                verification_status=JDSkillVerificationStatus.AUTO_VERIFIED,
            ))
        for name in PREFERRED_SKILLS:
            db.add(JDSkill(
                id=uuid.uuid4(), jd_id=jd.id, canonical_skill_id=skill_ids[name],
                mandatory=False, weight=1.00, confidence=1.0, match_tier="EXACT",
                verification_status=JDSkillVerificationStatus.AUTO_VERIFIED,
            ))
        print(f"Added {len(REQUIRED_SKILLS) + len(PREFERRED_SKILLS)} jd_skills rows")

        # --- 2. Hiring campaign ---------------------------------------------------
        campaign = HiringCampaign(
            id=uuid.uuid4(),
            org_id=ORG_ID,
            jd_id=jd.id,
            name=SEED_CAMPAIGN_NAME,
            weight_deterministic=30.00,
            weight_semantic=40.00,
            weight_ai=30.00,
            semantic_threshold=0.6500,
            ai_threshold=50.00,
            deterministic_threshold=70.00,
            max_candidates=20,
            prompt_template_id=resume_parse_prompt_template_id,
            hiring_manager_id=HIRING_MANAGER_ID,
            recruiter_id=RECRUITER_ID,
            created_by=HR_ADMIN_ID,
        )
        db.add(campaign)
        db.flush()
        print(f"Added hiring_campaign: {campaign.name} (id={campaign.id})")

        # --- 3. Candidates / resumes / campaign_candidates / stage history ------
        now = datetime.now(timezone.utc)
        for i, (target_stage, composite_score, ai_recommendation, is_fraud_flagged) in enumerate(CANDIDATE_PLAN, start=1):
            full_name = f"[SEED] Candidate {i}"
            email = f"seed.candidate{i}.m12@example.test"
            full_name_ct, key_id_1 = encryption_service.encrypt(full_name, purpose="CANDIDATE_PII")
            email_ct, key_id_2 = encryption_service.encrypt(email, purpose="CANDIDATE_PII")

            candidate = Candidate(
                id=uuid.uuid4(),
                org_id=ORG_ID,
                full_name_encrypted=full_name_ct,
                email_encrypted=email_ct,
                email_hash=encryption_service.generate_hash(email),
                encryption_key_id=key_id_1,
                jurisdiction="INDIA",
                consent_given=True,
                consent_timestamp=now,
                consent_source="SEED_DATA",
                source_campaign_id=None,
            )
            db.add(candidate)
            db.flush()

            resume = Resume(
                id=uuid.uuid4(),
                candidate_id=candidate.id,
                file_path=f"seed/m12/candidate_{i}_resume.pdf",
                file_format=FileFormat.PDF,
                file_hash=hashlib.sha256(f"seed-m12-resume-{i}".encode("utf-8")).hexdigest(),
                original_filename=f"seed_candidate_{i}_resume.pdf",
                parse_status=ParseStatus.PARSED,
                uploaded_by=HR_ADMIN_ID,
            )
            db.add(resume)
            db.flush()

            idempotency_key = _campaign_candidate_idempotency_key(campaign.id, candidate.id, resume.id)
            cc = CampaignCandidate(
                id=uuid.uuid4(),
                campaign_id=campaign.id,
                candidate_id=candidate.id,
                resume_id=resume.id,
                idempotency_key=idempotency_key,
                pipeline_stage=target_stage,
                composite_score=composite_score,
                composite_score_computed_at=now if composite_score is not None else None,
                ai_recommendation=ai_recommendation,
                is_fraud_flagged=is_fraud_flagged,
                fraud_flags=(
                    {
                        "flags": ["NEAR_DUPLICATE_RESUME"],
                        "detected_at": now.isoformat(),
                        "detector": "SYSTEM",
                        "details": {
                            "similarity_score": 0.94,
                            "duplicate_of_candidate_id": None,
                            "note": "Resume text closely matches another submission; pending HR_ADMIN review.",
                        },
                    }
                    if is_fraud_flagged else None
                ),
            )
            db.add(cc)
            db.flush()

            # Always: initial creation row, matching the real convention
            # (from_stage=NULL -> to_stage=UPLOADED, SYSTEM, changed_by=NULL).
            db.add(CampaignCandidateStageHistory(
                id=uuid.uuid4(), campaign_candidate_id=cc.id,
                from_stage=None, to_stage=PipelineStage.UPLOADED,
                changed_by=None, transition_source=TransitionSource.SYSTEM,
            ))

            target_index = STAGE_PATH.index(target_stage)
            for step in range(target_index):
                from_stage, to_stage = STAGE_PATH[step], STAGE_PATH[step + 1]
                is_manual_hm_hop = to_stage == PipelineStage.INTERVIEW
                db.add(CampaignCandidateStageHistory(
                    id=uuid.uuid4(), campaign_candidate_id=cc.id,
                    from_stage=from_stage, to_stage=to_stage,
                    changed_by=HIRING_MANAGER_ID if is_manual_hm_hop else None,
                    transition_source=TransitionSource.MANUAL if is_manual_hm_hop else TransitionSource.SYSTEM,
                    change_reason="Approved for interview after review." if is_manual_hm_hop else None,
                ))

            print(f"Added candidate {i}: stage={target_stage.value} composite_score={composite_score} fraud={is_fraud_flagged}")

        db.commit()
        print("\nM12 seed campaign complete")

except Exception as e:
    db.rollback()
    print(f"Error seeding M12 test campaign: {e}")
    raise

finally:
    db.close()
