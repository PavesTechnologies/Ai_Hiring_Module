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
    # Resume Intake (M05) — RESUME_UPLOADED/CONSENT_RECORDED/
    # UPLOAD_BLOCKED_ERASURE_REQUEST are all already present in the live
    # audit_action_type_enum (re-verified directly against the DB) — usable
    # now, unlike the note below.
    RESUME_UPLOADED = "RESUME_UPLOADED"
    CONSENT_RECORDED = "CONSENT_RECORDED"
    UPLOAD_BLOCKED_ERASURE_REQUEST = "UPLOAD_BLOCKED_ERASURE_REQUEST"
    # Resume Intake (M05) Phase 11 — the DB-side audit_action_type_enum does
    # NOT yet contain this value (verified against the live DB). Writing an
    # AuditLog row with it will fail with "invalid input value for enum"
    # until `ALTER TYPE audit_action_type_enum ADD VALUE 'CIRCUIT_BREAKER_OPENED'`
    # is run against the database (see the CAMPAIGN_RESUMED precedent in
    # alembic/versions/d5c1a0b2e3f4_pause_campaign_support.py).
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
    # Epic 3 (M05-E03) Phase C0 — the DB-side audit_action_type_enum does NOT
    # yet contain this value (verified against the live DB). Needs
    # `ALTER TYPE audit_action_type_enum ADD VALUE 'PIPELINE_STAGE_TRANSITIONED'`
    # before PipelineTransitionService can log it — see the migration added
    # alongside this Python member.
    PIPELINE_STAGE_TRANSITIONED = "PIPELINE_STAGE_TRANSITIONED"
    # Epic 3 (M05-E03) Phase C4 — same DB-enum caveat as PIPELINE_STAGE_TRANSITIONED
    # above; needs `ALTER TYPE audit_action_type_enum ADD VALUE
    # 'CAMPAIGN_RESUBMISSION_DETECTED'` before ResubmissionAlertService can log it.
    CAMPAIGN_RESUBMISSION_DETECTED = "CAMPAIGN_RESUBMISSION_DETECTED"
    # M07-E03 S03 T03
    REJECTED_CANDIDATES_EXPORTED = "REJECTED_CANDIDATES_EXPORTED"
    # M07-E03 S04
    DETERMINISTIC_OVERRIDE_APPLIED = "DETERMINISTIC_OVERRIDE_APPLIED"
    OVERRIDE_REPORT_EXPORTED = "OVERRIDE_REPORT_EXPORTED"
    # M07-E03 S05
    DETERMINISTIC_ANALYTICS_EXPORTED = "DETERMINISTIC_ANALYTICS_EXPORTED"
    # NOTE: same DB-enum caveat as RESUME_UPLOADED/CIRCUIT_BREAKER_OPENED
    # above - needs `ALTER TYPE audit_action_type_enum ADD VALUE
    # 'UNKNOWN_SKILL_DELETED'` (see alembic/versions/<new>_unknown_skill_deleted_audit_action.py)
    # before this can actually be written to audit_log.
    UNKNOWN_SKILL_DELETED = "UNKNOWN_SKILL_DELETED"
    BULK_UPLOAD_FILE_REPLAYED = "BULK_UPLOAD_FILE_REPLAYED"
    # Epic 4 (M05-E04) Phase D0 — the DB-side audit_action_type_enum does NOT
    # yet contain these 4 values (this migration adds them in the same
    # phase). Writing an AuditLog row with any of these will fail with
    # "invalid input value for enum" until the paired migration
    # (alembic/versions/<new>_audit_enum_upload_tracking_values.py) is
    # applied against the database.
    UPLOAD_HISTORY_EXPORTED = "UPLOAD_HISTORY_EXPORTED"
    RESUME_UPLOAD_RETRIED = "RESUME_UPLOAD_RETRIED"
    INDIVIDUAL_UPLOAD_DLQ_REPLAYED = "INDIVIDUAL_UPLOAD_DLQ_REPLAYED"
    PLATFORM_ALERT_SENT = "PLATFORM_ALERT_SENT"
    # GDPR-style candidate hard delete — the DB-side audit_action_type_enum
    # does NOT yet contain this value (same caveat as the other entries
    # above); needs the paired migration
    # (alembic/versions/<new>_candidate_data_erased_audit_action.py) before
    # CandidateErasureService can log it.
    CANDIDATE_DATA_ERASED = "CANDIDATE_DATA_ERASED"

class EntityType(enum.Enum):
    JOB_DESCRIPTION= "JOB_DESCRIPTION"
    CAMPAIGN= "CAMPAIGN"
    CAMPAIGN_CANDIDATE = "CAMPAIGN_CANDIDATE"
    CAMPAIGN_WEIGHT_PRESET = "CAMPAIGN_WEIGHT_PRESET"
    PLATFORM_CONFIG = "PLATFORM_CONFIG"
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
    BULK_UPLOAD_JOB_FILE = "BULK_UPLOAD_JOB_FILE"
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
