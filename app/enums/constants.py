from enum import Enum
import enum
class UserRole(str, Enum):
    HR_ADMIN       = "HR_ADMIN"
    RECRUITER      = "RECRUITER"
    HIRING_MANAGER = "HIRING_MANAGER"
class PipelineStage(str, Enum):
    APPLIED       = "APPLIED"
    SCREENING     = "SCREENING"
    SHORTLISTED   = "SHORTLISTED"
    INTERVIEW     = "INTERVIEW"
    OFFER         = "OFFER"
    HIRED         = "HIRED"
    REJECTED      = "REJECTED"
class Jurisdiction(str, Enum):
    GLOBAL = "GLOBAL"
    EU     = "EU"
    US     = "US"
    IN     = "IN"   
class ActionType(enum.Enum):
    JD_CREATED= "JD_CREATED"
    JD_UPDATED= "JD_UPDATED"
    JD_VERSION_CREATED= "JD_VERSION_CREATED"
    JD_CLOSED= "JD_CLOSED"
    JD_EXPORTED= "JD_EXPORTED"
    CAMPAIGN_CREATED= "CAMPAIGN_CREATED"
    CAMPAIGN_UPDATED= "CAMPAIGN_UPDATED"
    CAMPAIGN_SCORING_CONFIG_CHANGED = "CAMPAIGN_SCORING_CONFIG_CHANGED"
    CAMPAIGN_THRESHOLDS_UPDATED = "CAMPAIGN_THRESHOLDS_UPDATED"
    HIRING_MANAGER_REASSIGNED = "HIRING_MANAGER_REASSIGNED"
    CAMPAIGN_PAUSED= "CAMPAIGN_PAUSED"
    CAMPAIGN_RESUMED= "CAMPAIGN_RESUMED"
    CAMPAIGN_CLOSED= "CAMPAIGN_CLOSED"
    CAMPAIGN_REOPENED = "CAMPAIGN_REOPENED"
    CAMPAIGN_DUPLICATED = "CAMPAIGN_DUPLICATED"
    CAMPAIGN_HEALTH_ALERT = "CAMPAIGN_HEALTH_ALERT"
    CAMPAIGN_ACTIVATED= "CAMPAIGN_ACTIVATED"
    CAMPAIGN_AUTO_CLOSED = "CAMPAIGN_AUTO_CLOSED"
    CAMPAIGN_EDIT_BLOCKED = "CAMPAIGN_EDIT_BLOCKED"
    CAMPAIGN_WEIGHT_PRESET_CREATED = "CAMPAIGN_WEIGHT_PRESET_CREATED"
    CAMPAIGN_WEIGHT_PRESET_UPDATED = "CAMPAIGN_WEIGHT_PRESET_UPDATED"
    CAMPAIGN_WEIGHT_PRESET_DELETED = "CAMPAIGN_WEIGHT_PRESET_DELETED"
    CAMPAIGN_SCORING_CONFIG_COPIED = "CAMPAIGN_SCORING_CONFIG_COPIED"
    PLATFORM_CONFIG_UPDATED = "PLATFORM_CONFIG_UPDATED"
    CANDIDATE_ADDED = "CANDIDATE_ADDED"
    CANDIDATE_UPDATED = "CANDIDATE_UPDATED"
    CANDIDATE_REMOVED = "CANDIDATE_REMOVED"
    DETERMINISTIC_SCORE_COMPUTED = "DETERMINISTIC_SCORE_COMPUTED"
    JD_REPROCESSED = "JD_REPROCESSED"
    UNKNOWN_SKILL_CREATED = "UNKNOWN_SKILL_CREATED"
    UNKNOWN_SKILL_MAPPED = "UNKNOWN_SKILL_MAPPED"
    UNKNOWN_SKILL_PROMOTED = "UNKNOWN_SKILL_PROMOTED"
    UNKNOWN_SKILL_DISMISSED = "UNKNOWN_SKILL_DISMISSED"
    JD_SKILL_REMAPPED = "JD_SKILL_REMAPPED"
    ALIAS_ADDED = "ALIAS_ADDED"
    RESUME_UPLOADED = "RESUME_UPLOADED"
    CONSENT_RECORDED = "CONSENT_RECORDED"
    UPLOAD_BLOCKED_ERASURE_REQUEST = "UPLOAD_BLOCKED_ERASURE_REQUEST"
    CIRCUIT_BREAKER_OPENED = "CIRCUIT_BREAKER_OPENED"
    BULK_UPLOAD_CANCELLED = "BULK_UPLOAD_CANCELLED"
    BULK_UPLOAD_HISTORY_EXPORTED = "BULK_UPLOAD_HISTORY_EXPORTED"
    SKILL_UPDATED = "SKILL_UPDATED"
    ALIAS_DUPLICATE_DETECTED = "ALIAS_DUPLICATE_DETECTED"
    SKILL_PARENT_UPDATED = "SKILL_PARENT_UPDATED"
    SKILL_DEACTIVATED = "SKILL_DEACTIVATED"
    SKILL_REACTIVATED = "SKILL_REACTIVATED"
    RESUME_PARSED = "RESUME_PARSED"
    RESUME_PARSE_FAILED = "RESUME_PARSE_FAILED"
    CANDIDATE_SKILL_MATCHED = "CANDIDATE_SKILL_MATCHED"
    PIPELINE_STAGE_TRANSITIONED = "PIPELINE_STAGE_TRANSITIONED"
    CAMPAIGN_RESUBMISSION_DETECTED = "CAMPAIGN_RESUBMISSION_DETECTED"
    REJECTED_CANDIDATES_EXPORTED = "REJECTED_CANDIDATES_EXPORTED"
    DETERMINISTIC_OVERRIDE_APPLIED = "DETERMINISTIC_OVERRIDE_APPLIED"
    OVERRIDE_REPORT_EXPORTED = "OVERRIDE_REPORT_EXPORTED"
    DETERMINISTIC_ANALYTICS_EXPORTED = "DETERMINISTIC_ANALYTICS_EXPORTED"
    UNKNOWN_SKILL_DELETED = "UNKNOWN_SKILL_DELETED"
    BULK_UPLOAD_FILE_REPLAYED = "BULK_UPLOAD_FILE_REPLAYED"
    SEMANTIC_SCORE_COMPUTED = "SEMANTIC_SCORE_COMPUTED"

    DLQ_TASK_REPLAYED = "DLQ_TASK_REPLAYED" 
    # stalled-candidate actions + report exports
    STALLED_CANDIDATES_ALERT = "STALLED_CANDIDATES_ALERT"
    CANDIDATE_STALL_ESCALATED = "CANDIDATE_STALL_ESCALATED"
    CANDIDATE_STAGE_OVERRIDDEN = "CANDIDATE_STAGE_OVERRIDDEN"
    CANDIDATE_FLAGGED_FOR_REVIEW = "CANDIDATE_FLAGGED_FOR_REVIEW"
    REJECTION_REPORT_EXPORTED = "REJECTION_REPORT_EXPORTED"
    CAMPAIGN_SUMMARY_EXPORTED = "CAMPAIGN_SUMMARY_EXPORTED"
    # Epic 4 (M05-E04) Phase D0 — the DB-side audit_action_type_enum does NOT
    # yet contain these 4 values (this migration adds them in the same
    # phase). Writing an AuditLog row with any of these will fail with
    # "invalid input value for enum" until the paired migration
    # (alembic/versions/a9d4f2c7e6b3_audit_enum_upload_tracking_values.py) is
    # applied against the database.
    UPLOAD_HISTORY_EXPORTED = "UPLOAD_HISTORY_EXPORTED"
    RESUME_UPLOAD_RETRIED = "RESUME_UPLOAD_RETRIED"
    INDIVIDUAL_UPLOAD_DLQ_REPLAYED = "INDIVIDUAL_UPLOAD_DLQ_REPLAYED"
    PLATFORM_ALERT_SENT = "PLATFORM_ALERT_SENT"
    # GDPR-style candidate hard delete — the DB-side audit_action_type_enum
    # does NOT yet contain this value (same caveat as the other entries
    # above); needs the paired migration
    # (alembic/versions/90b05f9f2aa1_candidate_data_erased_audit_action.py)
    # before CandidateErasureService can log it.
    CANDIDATE_DATA_ERASED = "CANDIDATE_DATA_ERASED"
    # Single-resume cleanup (stuck/orphaned resumes, e.g. a task that was
    # enqueued but never picked up) — the DB-side audit_action_type_enum
    # does NOT yet contain this value; needs the paired migration
    # (alembic/versions/<new>_resume_deleted_audit_action.py) before
    # ResumeCleanupService can log it.
    RESUME_DELETED = "RESUME_DELETED"
    # Prompt Management (AIRS)
    PROMPT_CREATED = "PROMPT_CREATED"
    PROMPT_UPDATED = "PROMPT_UPDATED"
    PROMPT_DELETED = "PROMPT_DELETED"
    PROMPT_STATUS_CHANGED = "PROMPT_STATUS_CHANGED"
    # M10-E01 — the DB-side audit_action_type_enum does NOT yet contain this
    # value (same caveat as every other entry above); needs the paired
    # migration (alembic/versions/<new>_composite_scoring_support.py) before
    # CompositeScoringService can log it.
    COMPOSITE_SCORE_COMPUTED = "COMPOSITE_SCORE_COMPUTED"
    # M10-E02 — same DB-enum caveat as COMPOSITE_SCORE_COMPUTED above; needs
    # the paired migration (alembic/versions/<new>_campaign_weight_configuration_history.py)
    # before CampaignService can log it. Written whenever a campaign's
    # weight_deterministic/weight_semantic/weight_ai actually change (never
    # on a no-op resubmission of identical weights, and never for a
    # thresholds-only change - see CampaignService._record_weight_configuration_change).
    CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED = "CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED"

class EntityType(enum.Enum):
    JOB_DESCRIPTION= "JOB_DESCRIPTION"
    CAMPAIGN= "CAMPAIGN"
    CAMPAIGN_CANDIDATE = "CAMPAIGN_CANDIDATE"
    CAMPAIGN_WEIGHT_PRESET = "CAMPAIGN_WEIGHT_PRESET"
    PLATFORM_CONFIG = "PLATFORM_CONFIG"
    SKILL_ONTOLOGY = "SKILL_ONTOLOGY"
    UNKNOWN_SKILL = "UNKNOWN_SKILL"
    JD_SKILL = "JD_SKILL"

    CANDIDATE = "CANDIDATE"
    RESUME = "RESUME"
    CONSENT = "CONSENT"

    BULK_UPLOAD_JOB = "BULK_UPLOAD_JOB"
    BULK_UPLOAD_JOB_FILE = "BULK_UPLOAD_JOB_FILE"
    CANDIDATE_SKILL = "CANDIDATE_SKILL"
    DEAD_LETTER_QUEUE = "DEAD_LETTER_QUEUE"
    # Referenced by resume_upload_service's breaker-opened audit since M05
    # Phase 11 but never actually defined here — that call silently failed
    # inside its own try/except. Added so the audit entry actually gets written.
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    PROMPT_TEMPLATE = "PROMPT_TEMPLATE"


S3_RESUME_PREFIX = "resumes/"
S3_JD_PREFIX     = "job-descriptions/"

EMBEDDING_DIM = 384

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE     = 100


API_PREFIX = "/airs"

# M10-E01: version tag stamped on every composite_score calculation
# (campaign_candidates.composite_score and every
# candidate_composite_score_history row). Bump this — and only this — the
# moment CompositeScoringService's formula itself changes; never hardcode
# "v1" (or any other version string) anywhere else in the codebase.
COMPOSITE_SCORE_FORMULA_VERSION = "v1"
