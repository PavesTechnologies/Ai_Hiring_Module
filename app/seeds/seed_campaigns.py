import uuid
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.identity import User
from app.models.jd.job_descriptions import JobDescription

db = SessionLocal()

try:
    jd = db.query(JobDescription).filter(JobDescription.is_active_version.is_(True)).first()
    if not jd:
        raise RuntimeError("No active JobDescription found in the database — seed a JD first.")

    user = db.query(User).first()
    if not user:
        raise RuntimeError("No User found in the database — seed a user first.")

    campaigns = [
        HiringCampaign(
            id=uuid.uuid4(),
            org_id=None,
            jd_id=jd.id,
            name="Backend Engineer - Test Campaign",
            status=CampaignStatus.ACTIVE,
            weight_deterministic=30.00,
            weight_semantic=40.00,
            weight_ai=30.00,
            semantic_threshold=0.6500,
            ai_threshold=50.00,
            max_candidates=50,
            deadline=datetime.now(timezone.utc) + timedelta(days=30),
            hiring_manager_id=user.id,
            recruiter_id=user.id,
            created_by=user.id,
        ),
        HiringCampaign(
            id=uuid.uuid4(),
            org_id=None,
            jd_id=jd.id,
            name="Frontend Engineer - Test Campaign",
            status=CampaignStatus.ACTIVE,
            weight_deterministic=30.00,
            weight_semantic=40.00,
            weight_ai=30.00,
            semantic_threshold=0.6500,
            ai_threshold=50.00,
            max_candidates=None,
            deadline=None,
            hiring_manager_id=user.id,
            recruiter_id=user.id,
            created_by=user.id,
        ),
        HiringCampaign(
            id=uuid.uuid4(),
            org_id=None,
            jd_id=jd.id,
            name="Paused Campaign - Test",
            status=CampaignStatus.PAUSED,
            weight_deterministic=30.00,
            weight_semantic=40.00,
            weight_ai=30.00,
            semantic_threshold=0.6500,
            ai_threshold=50.00,
            max_candidates=None,
            deadline=None,
            hiring_manager_id=user.id,
            recruiter_id=user.id,
            created_by=user.id,
        ),
    ]

    for campaign in campaigns:
        existing = (
            db.query(HiringCampaign)
            .filter(HiringCampaign.name == campaign.name)
            .first()
        )
        if not existing:
            db.add(campaign)
            print(f"Added campaign: {campaign.name} ({campaign.status.value}) id={campaign.id}")
        else:
            print(f"Campaign already exists: {campaign.name} (id={existing.id})")

    db.commit()
    print("\nCampaigns seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding campaigns: {e}")
    raise

finally:
    db.close()
