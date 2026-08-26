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
        # M07-E02: Experience & Education Validation config
        PlatformConfig(
            id=uuid.uuid4(),
            key="EXPERIENCE_TOLERANCE_YEARS",
            value="0.0",
            description="Years a candidate's total experience may fall short of a JD's min_experience_years and still pass",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EQUIVALENT_EXPERIENCE_YEARS",
            value="8.0",
            description="Total years of experience that substitute for an insufficient/missing degree in education validation",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DETERMINISTIC_WEIGHT_SKILLS",
            value="0.70",
            description="Weight of the skill-based sub-score in the combined deterministic_score blend (with experience/education)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DETERMINISTIC_WEIGHT_EXPERIENCE",
            value="0.15",
            description="Weight of the experience validation sub-score in the combined deterministic_score blend",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DETERMINISTIC_WEIGHT_EDUCATION",
            value="0.15",
            description="Weight of the education validation sub-score in the combined deterministic_score blend",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="CORE_IMPORTANCE_WEIGHT_MULTIPLIER",
            value="1.0",
            description="Multiplier applied to a required JD skill's effective weight when AI-classified as 'core'. 1.0 (neutral, no differentiation from supporting) until deliberately changed.",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER",
            value="1.0",
            description="Multiplier applied to a required JD skill's effective weight when AI-classified as 'supporting'. 1.0 (neutral, no differentiation from core) until deliberately changed.",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="OVERRIDE_RATE_ALERT_THRESHOLD",
            value="20",
            description="Override rate (%, overrides / rejected candidates) above which a campaign is flagged override_alert in the Override Report",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SKILL_MISMATCH_RATE_THRESHOLD",
            value="60",
            description="If a single mandatory skill is MISSING in more than this % of a campaign's deterministic rejections, recommend making it preferred instead of mandatory",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EXPERIENCE_ONLY_RATE_THRESHOLD",
            value="40",
            description="If more than this % of a campaign's deterministic rejections are experience-only failures, recommend reducing minimum experience or increasing tolerance",
        ),
        # Unknown Skill Suggestion (HR_ADMIN manual verification) config
        PlatformConfig(
            id=uuid.uuid4(),
            key="UNKNOWN_SKILL_SUGGESTION_TOP_K",
            value="10",
            description="Max number of suggestions returned per Unknown Skill suggestion endpoint (RapidFuzz/semantic x canonical/alias)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="UNKNOWN_SKILL_SUGGESTION_RAPIDFUZZ_THRESHOLD",
            value="85.00",
            description="RapidFuzz similarity score (0-100) above which a canonical/alias suggestion is considered a strong match",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="UNKNOWN_SKILL_SUGGESTION_SEMANTIC_THRESHOLD",
            value="0.80",
            description="Cosine similarity (0.0-1.0) above which a canonical/alias semantic suggestion is considered a strong match"
            ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DEADLINE_CHECK_INTERVAL_HOURS",
            value="1",
            description=(
                "Hours between Celery Beat runs of the deadline-based campaign "
                "auto-close task. Read once at Celery process startup — changing "
                "this value requires restarting the beat process to take effect."
            ),
        ),
        # E04-S01-T03: campaign pipeline health-alert thresholds
        PlatformConfig(
            id=uuid.uuid4(),
            key="DEAD_TASK_ALERT_THRESHOLD",
            value="5",
            description="DEAD celery_task_log count for a campaign above which a CAMPAIGN_HEALTH_ALERT is raised",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DETERMINISTIC_REJECTION_ALERT_THRESHOLD",
            value="80.00",
            description="Deterministic-layer rejection rate (%, 0-100) above which a CAMPAIGN_HEALTH_ALERT is raised",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SCREENING_SLA_HOURS",
            value="48",
            description="Average hours a campaign's currently-SCREENING candidates may sit before a CAMPAIGN_HEALTH_ALERT is raised",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="FRAUD_ALERT_THRESHOLD",
            value="3",
            description="FRAUD_REVIEW candidate count for a campaign above which a CAMPAIGN_HEALTH_ALERT is raised",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBEDDING_BATCH_SIZE",
            value="32",
            description="Batch size for SentenceTransformer.encode() calls in EMBED_RESUME (M08-E01 resume embedding generation)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MAX_DLQ_REPLAYS_PER_TASK",
            value="3",
            description="Maximum times a dead-lettered task chain may be replayed before further replays are blocked (M04-E04-S03-T02 infinite-loop guard)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="INTERVIEW_SLA_DAYS",
            value="7",
            description="Days a candidate may sit in INTERVIEW before being flagged as stalled (M04-E04-S04)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DASHBOARD_LOAD_TIMEOUT_SECONDS",
            value="10",
            description="Per-section dashboard fetch timeout before that section shows its error state (M11-E01-S05)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EXPORT_ASYNC_THRESHOLD",
            value="500",
            description="Candidate count above which an export is generated by the EXPORT_GENERATE Celery task instead of inline in the request (M11-E05-S01-T03)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EXPORT_LINK_EXPIRY_HOURS",
            value="24",
            description="Lifetime of a generated export's signed download link (M11-E05-S01-T03)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DETERMINISTIC_HIGH_REJECTION_THRESHOLD",
            value="60.00",
            description="DETERMINISTIC-layer rejection rate (%, of total rejections) above which the rejection-analytics panel recommends reviewing JD mandatory skills (M04-E04-S05-T02)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SEMANTIC_HIGH_REJECTION_THRESHOLD",
            value="40.00",
            description="SEMANTIC-layer rejection rate (%) above which the rejection-analytics panel recommends lowering semantic_threshold (M04-E04-S05-T02)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="AI_HIGH_REJECTION_THRESHOLD",
            value="40.00",
            description="AI-layer rejection rate (%) above which the rejection-analytics panel recommends lowering ai_threshold (M04-E04-S05-T02)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MIN_CANDIDATES_FOR_ANALYTICS",
            value="10",
            description="Minimum processed candidates before rejection-analytics recommendations are shown (M04-E04-S05-T02)",
        ),
        # Epic 4 (M05-E04) Phase D0: platform-wide upload-queue alert thresholds
        PlatformConfig(
            id=uuid.uuid4(),
            key="QUEUE_BACKLOG_ALERT_THRESHOLD",
            value="100",
            description="Total QUEUED RESUME_DOCUMENT_PROCESSING tasks platform-wide above which a PLATFORM_ALERT_SENT alert is raised (D13)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="PARSE_DURATION_ALERT_THRESHOLD_MS",
            value="120000",
            description="Average RESUME_DOCUMENT_PROCESSING task duration (ms) in the last hour above which a degraded-performance alert is raised (D13)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="DAILY_DEAD_TASK_ALERT_THRESHOLD",
            value="10",
            description="DEAD task count in the last 24 hours, platform-wide, above which an alert is raised (D13) - distinct from the campaign-scoped DEAD_TASK_ALERT_THRESHOLD",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="ALERT_COOLDOWN_HOURS",
            value="12",
            description="Minimum hours between repeat platform-upload alerts for the same condition (D13)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MAX_AI_RETRY_COUNT",
            value="3",
            description="Descriptive value for upload-failure notification copy (D11) - matches the real hardcoded retry_policy.py DEFAULT_POLICY.max_attempts; does not itself control retry behavior",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="JD_EMBEDDING_MAX_CHARS",
            value="2000",
            description="Max characters of a JD's raw_text included in the JD embedding input (M08-E01 S02 JD embedding generation) - only the raw_text portion is truncated, never the title or skill lists",
        ),
        # EMBED_RESUME resilient retry / circuit breaker
        PlatformConfig(
            id=uuid.uuid4(),
            key="MAX_EMBED_RETRY_COUNT",
            value="4",
            description="Max real embedding-call attempts for EMBED_RESUME before dead-lettering and setting ai_evaluation_status=MANUAL_REVIEW - matches the 4-step 30/60/120/240s backoff below",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBED_RETRY_BASE_DELAY_SECONDS",
            value="30",
            description="First backoff delay for a transient EMBED_RESUME failure (30, 60, 120, 240s doubling up to EMBED_RETRY_MAX_DELAY_SECONDS)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBED_RETRY_MAX_DELAY_SECONDS",
            value="240",
            description="Backoff delay ceiling for a transient EMBED_RESUME failure",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
            value="10",
            description="Consecutive EMBED_RESUME failures before the EMBEDDING_SERVICE circuit breaker opens (circuit_breaker_state.service_name='EMBEDDING_SERVICE')",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBEDDING_FAILURE_ALERT_THRESHOLD",
            value="20.00",
            description="Percentage of SCREENING candidates with a NULL semantic_score (not yet flagged MANUAL_REVIEW) in a campaign above which the embedding-health monitor emails HR_ADMIN",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="EMBEDDING_REINDEX_THRESHOLD",
            value="50000",
            description="Once resume_embeddings row count exceeds this, the Embedding Storage Dashboard shows a warning and queues REINDEX_IVFFLAT to rebuild idx_resume_embeddings_embedding with better-tuned clustering",
        ),
        # M12: Workflow & Interview Scheduling config
        PlatformConfig(
            id=uuid.uuid4(),
            key="SHORTLIST_NOTIFICATION_BATCH_WINDOW_MINUTES",
            value="30",
            description="Minutes over which SHORTLISTED notifications are batched into a single email/digest before sending (M12)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="FRAUD_HIGH_RISK_SLA_DAYS",
            value="2",
            description="Days a high-risk FRAUD_REVIEW candidate may sit before an SLA breach alert is raised (M12)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="SINGLE_HIRE_PER_CAMPAIGN",
            value="true",
            description="Boolean (\"true\"/\"false\" string - parse via raw.lower() == \"true\", NOT bool(raw)) - whether a campaign auto-closes/blocks further SELECTED candidates once one hire is made (M12)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="INTERVIEW_MIN_NOTICE_HOURS",
            value="24",
            description="Minimum hours of advance notice required when scheduling an interview (M12)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="OAUTH_TOKEN_REFRESH_BUFFER_SECONDS",
            value="300",
            description="Seconds before an OAuth token's actual expiry at which it is proactively refreshed (M12 calendar/interview integrations)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="MAX_EMAIL_RETRY_COUNT",
            value="4",
            description="Max attempts for a transient interview/notification email send failure before dead-lettering (M12)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="RESUME_DOWNLOAD_URL_EXPIRY_SECONDS",
            value="300",
            description="Validity (seconds) of a server-generated signed URL for downloading a specific resume version (M07-E0x S02-T01)",
        ),
        PlatformConfig(
            id=uuid.uuid4(),
            key="RESUME_FRESHNESS_MAX_AGE_DAYS",
            value="180",
            description="Max age (days, ~6 months) of a resume version's created_at before ResumeSelectionService excludes it from Talent Pool campaign selection, regardless of is_talent_pool_eligible (M13-E01 Talent Pool Eligibility)",
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
