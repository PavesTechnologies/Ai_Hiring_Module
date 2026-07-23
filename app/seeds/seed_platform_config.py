import json
import uuid

from app.db.session import SessionLocal
from app.models.config import PlatformConfig

db = SessionLocal()

# Per-jurisdiction consent requirements consumed by the (future) ConsentService.
# Stored as a JSON string since platform_config.value is a plain String column
# (no migration available to make it JSONB) — parsed at the application layer.
_JURISDICTION_CONSENT_CONFIG = json.dumps({
    "GLOBAL": {
        "consent_version": "1.0",
        "min_acceptable_consent_version": "1.0",
        "consent_text_key": "consent_disclosure_global",
        "requires_explicit_opt_in": False,
    },
    "EU": {
        "consent_version": "1.0",
        "min_acceptable_consent_version": "1.0",
        "consent_text_key": "consent_disclosure_eu",
        "requires_explicit_opt_in": True,
    },
    "US": {
        "consent_version": "1.0",
        "min_acceptable_consent_version": "1.0",
        "consent_text_key": "consent_disclosure_us",
        "requires_explicit_opt_in": False,
    },
    "IN": {
        "consent_version": "1.0",
        "min_acceptable_consent_version": "1.0",
        "consent_text_key": "consent_disclosure_in",
        "requires_explicit_opt_in": True,
    },
})

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
        # Resume Intake (M05) / Consent (M16) config
        PlatformConfig(
            id=uuid.uuid4(),
            key="RESUME_MAX_SIZE_MB",
            value="10",
            description="Maximum accepted resume file size in MB for individual resume uploads",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CONSENT_VERSION",
            value="1.0",
            description="Current consent legal-text version applied to new consent captures by default",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="JURISDICTION_CONSENT_CONFIG",
            value=_JURISDICTION_CONSENT_CONFIG,
            description=(
                "JSON object keyed by jurisdiction (GLOBAL/EU/US/IN), each holding "
                "consent_version, min_acceptable_consent_version, consent_text_key, "
                "and requires_explicit_opt_in. Parsed by the application layer."
            ),
        ),
        # Bulk ZIP Upload (M05-E02) config
        PlatformConfig(
            id=uuid.uuid4(),
            key="ZIP_MAX_SIZE_MB",
            value="500",
            description="Maximum accepted ZIP archive size in MB for bulk resume uploads",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MAX_FILES_PER_ZIP",
            value="200",
            description=(
                "Maximum number of resume files processed from a single bulk-upload "
                "ZIP archive; extraction stops and the uploader is asked to split the "
                "batch if exceeded. Not specified by the epic — a reasonable, tunable "
                "default given ZIP_MAX_SIZE_MB=500 and typical resume file sizes."
            ),
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SKILL_SIMILARITY_THRESHOLD",
            value="90.00",
            description="RapidFuzz similarity score (0-100) above which a newly created skill is flagged as similar to an existing one",
        ),
        # S04-T03: campaign cap/deadline warning thresholds
        PlatformConfig(
            id=uuid.uuid4(),
            key="CAP_WARNING_PERCENTAGE",
            value="80.00",
            description="Candidate-cap percentage (0-100) at which a campaign is flagged as approaching_cap",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DEADLINE_WARNING_DAYS",
            value="3",
            description="Number of days before a campaign deadline at which it is flagged as deadline_soon",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="HM_REVIEW_SLA_DAYS",
            value="5",
            description="Days a candidate can sit in HM_REVIEW before the campaign is flagged overdue_review",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="STALE_CAMPAIGN_DAYS",
            value="7",
            description="Days without a new candidate before a campaign is flagged pipeline_stalled",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MIN_LAYER_WEIGHT",
            value="5.00",
            description="Minimum weight (%) any single scoring layer may be set to — prevents a layer from being configured to 0 and bypassed entirely",
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
