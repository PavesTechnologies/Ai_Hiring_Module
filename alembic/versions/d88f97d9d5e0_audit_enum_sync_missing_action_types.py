"""audit enum sync missing action types

ActionType (app/enums/constants.py) has accumulated 21 values over several
features (campaign duplication/reopen/weight-presets/thresholds, DLQ replay,
consent, circuit breaker, etc.) that were never added to the Postgres
audit_action_type_enum type - e.g. AuditService.log(action_type=CAMPAIGN_DUPLICATED)
was failing with psycopg2.errors.InvalidTextRepresentation.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2 / f7c1a4d8b3e6) - transactional_ddl = False below, same as
those migrations.

Revision ID: d88f97d9d5e0
Revises: 8045be7751f1
Create Date: 2026-07-31

"""
from alembic import op

revision = "d88f97d9d5e0"
down_revision = "8045be7751f1"
branch_labels = None
depends_on = None

transactional_ddl = False

MISSING_VALUES = [
    "CAMPAIGN_DUPLICATED",
    "CAMPAIGN_REOPENED",
    "CAMPAIGN_WEIGHT_PRESET_CREATED",
    "CAMPAIGN_WEIGHT_PRESET_UPDATED",
    "CAMPAIGN_WEIGHT_PRESET_DELETED",
    "CAMPAIGN_THRESHOLDS_UPDATED",
    "CAMPAIGN_SCORING_CONFIG_COPIED",
    "CAMPAIGN_HEALTH_ALERT",
    "CAMPAIGN_SUMMARY_EXPORTED",
    "CANDIDATE_STALL_ESCALATED",
    "CANDIDATE_FLAGGED_FOR_REVIEW",
    "CANDIDATE_STAGE_OVERRIDDEN",
    "REJECTION_REPORT_EXPORTED",
    "DETERMINISTIC_SCORE_COMPUTED",
    "HIRING_MANAGER_REASSIGNED",
    "BULK_UPLOAD_FILE_REPLAYED",
    "CONSENT_RECORDED",
    "UPLOAD_BLOCKED_ERASURE_REQUEST",
    "PLATFORM_CONFIG_UPDATED",
    "STALLED_CANDIDATES_ALERT",
    "CIRCUIT_BREAKER_OPENED",
    "DLQ_TASK_REPLAYED",
]


def upgrade() -> None:
    for value in MISSING_VALUES:
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
