"""audit enum entity type sync

EntityType (app/enums/constants.py) has DEAD_LETTER_QUEUE, CAMPAIGN_WEIGHT_PRESET,
BULK_UPLOAD_JOB_FILE and CIRCUIT_BREAKER values that were never added to the
Postgres audit_entity_type_enum type - e.g. replay_dead_letter_tasks()'s
AuditService.log(entity_type=EntityType.DEAD_LETTER_QUEUE) was failing with
psycopg2.errors.InvalidTextRepresentation, so every DLQ replay (any task type)
died on the audit-log call after the replay itself already succeeded.

Same underlying drift class as d88f97d9d5e0 (which synced audit_action_type_enum
but not audit_entity_type_enum).

Revision ID: e4a9c1f6b8d3
Revises: d88f97d9d5e0
Create Date: 2026-08-05

"""
from alembic import op

revision = "e4a9c1f6b8d3"
down_revision = "d88f97d9d5e0"
branch_labels = None
depends_on = None

transactional_ddl = False

MISSING_VALUES = [
    "DEAD_LETTER_QUEUE",
    "CAMPAIGN_WEIGHT_PRESET",
    "BULK_UPLOAD_JOB_FILE",
    "CIRCUIT_BREAKER",
]


def upgrade() -> None:
    for value in MISSING_VALUES:
        op.execute(f"ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
