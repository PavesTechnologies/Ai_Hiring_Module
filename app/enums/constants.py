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
    CAMPAIGN_PAUSED= "CAMPAIGN_PAUSED"
    CAMPAIGN_RESUMED= "CAMPAIGN_RESUMED"
    CAMPAIGN_CLOSED= "CAMPAIGN_CLOSED"
    CAMPAIGN_ACTIVATED= "CAMPAIGN_ACTIVATED"
    CAMPAIGN_AUTO_CLOSED = "CAMPAIGN_AUTO_CLOSED"
    CAMPAIGN_EDIT_BLOCKED = "CAMPAIGN_EDIT_BLOCKED"
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
    # Resume Intake (M05) — NOTE: the DB-side audit_action_type_enum does not
    # yet contain these values (verified against the live DB). Writing an
    # AuditLog row with any of these will fail with "invalid input value for
    # enum" until `ALTER TYPE audit_action_type_enum ADD VALUE ...` is run
    # against the database (see the CAMPAIGN_RESUMED precedent in
    # alembic/versions/d5c1a0b2e3f4_pause_campaign_support.py). Needed before
    # Phase 7 actually logs a RESUME_UPLOADED event.
    RESUME_UPLOADED = "RESUME_UPLOADED"
    CONSENT_RECORDED = "CONSENT_RECORDED"
    UPLOAD_BLOCKED_ERASURE_REQUEST = "UPLOAD_BLOCKED_ERASURE_REQUEST"
    # Resume Intake (M05) Phase 11 — same DB-enum caveat as above: needs
    # `ALTER TYPE audit_action_type_enum ADD VALUE 'CIRCUIT_BREAKER_OPENED'`
    # before this can actually be written to audit_log.
    CIRCUIT_BREAKER_OPENED = "CIRCUIT_BREAKER_OPENED"
    # Bulk ZIP Upload (M05-E02) Phase B0 — added to the native Postgres
    # enum in the SAME migration that adds these Python members
    # (alembic/versions/a3f9c72e1b6d_bulk_zip_upload_schema.py), so these
    # are usable immediately, unlike the Resume Intake entries above.
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

class EntityType(enum.Enum):
    JOB_DESCRIPTION= "JOB_DESCRIPTION"
    CAMPAIGN= "CAMPAIGN"
    CAMPAIGN_CANDIDATE = "CAMPAIGN_CANDIDATE"
    SKILL_ONTOLOGY = "SKILL_ONTOLOGY"
    UNKNOWN_SKILL = "UNKNOWN_SKILL"
    JD_SKILL = "JD_SKILL"
    # Resume Intake (M05) — already present in the live audit_entity_type_enum;
    # kept in sync here so app.services.audit_service can write these entity types.
    CANDIDATE = "CANDIDATE"
    RESUME = "RESUME"
    CONSENT = "CONSENT"
    # Bulk ZIP Upload (M05-E02) Phase B0 — also already in the live enum.
    BULK_UPLOAD_JOB = "BULK_UPLOAD_JOB"
    CANDIDATE_SKILL = "CANDIDATE_SKILL"


# Resume storage prefix inside the S3 bucket
S3_RESUME_PREFIX = "resumes/"
S3_JD_PREFIX     = "job-descriptions/"

# Embedding dimensions for all-MiniLM-L6-v2
EMBEDDING_DIM = 384

# Default pagination
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE     = 100

# API prefix — all routes must be registered under this
API_PREFIX = "/airs"
