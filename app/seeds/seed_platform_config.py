import uuid

from app.db.session import SessionLocal
from app.models.config import PlatformConfig

db = SessionLocal()

try:
    # Default campaign scoring weights
    configs = [
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAMPAIGN_WEIGHT_DETERMINISTIC",
            value="30.00",
            description="Default deterministic scoring weight for campaigns (must sum to 100 with semantic and AI)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAMPAIGN_WEIGHT_SEMANTIC",
            value="40.00",
            description="Default semantic scoring weight for campaigns (must sum to 100 with deterministic and AI)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAMPAIGN_WEIGHT_AI",
            value="30.00",
            description="Default AI scoring weight for campaigns (must sum to 100 with deterministic and semantic)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SEMANTIC_PASS_THRESHOLD",
            value="0.6500",
            description="Default semantic similarity threshold for candidate screening (0.0 to 1.0)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="AI_PASS_THRESHOLD",
            value="50.00",
            description="Default AI scoring threshold for candidate screening (0 to 100)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAMPAIGN_AUTO_CLOSE_HOUR",
            value="0",
            description="Hour when Celery Beat automatically closes expired campaigns",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAMPAIGN_AUTO_CLOSE_MINUTE",
            value="0",
            description="Minute when Celery Beat automatically closes expired campaigns",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SKILL_SIMILARITY_THRESHOLD",
            value="90.00",
            description="RapidFuzz similarity score (0-100) above which a newly created skill is flagged as similar to an existing one",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="HIERARCHY_GRANDCHILD_MULTIPLIER",
            value="0.50",
            description="Deterministic mandatory-skill scoring: contribution multiplier for a GRANDCHILD (depth-2) hierarchy match",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="HIERARCHY_SEMANTIC_ONLY_THRESHOLD",
            value="0.80",
            description="Deterministic mandatory-skill scoring: minimum cosine similarity for a candidate skill to count as a SEMANTIC match against a missing mandatory skill",
        ),
    ]

    for config in configs:
        # Check if key already exists
        existing = db.query(PlatformConfig).filter(PlatformConfig.key == config.key).first()
        if not existing:
            db.add(config)
            print(f"Added config: {config.key} = {config.value}")
        else:
            print(f"Config already exists: {config.key}")

    db.commit()
    print("\nPlatform config seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding platform config: {e}")
    raise

finally:
    db.close()
