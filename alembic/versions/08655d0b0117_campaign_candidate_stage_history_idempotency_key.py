"""campaign_candidate_stage_history idempotency_key

E02 (Stage Transition Rules & Enforcement): StageTransitionService.transition()
needs to detect a retried/duplicate transition request and return the
existing history row instead of writing a second one - the same SAVEPOINT +
IntegrityError-catch pattern already used by
campaign_candidate_repository.create_idempotent() for campaign_candidates.
That pattern depends entirely on a real unique constraint to lose the race
against; campaign_candidate_stage_history had no such column at all
(confirmed via schema check - only the PK on `id`).

Nullable + partial unique index (WHERE idempotency_key IS NOT NULL): every
existing row (SYSTEM-driven writes made before this column existed, and any
future non-idempotent internal write) keeps a NULL value rather than being
forced to collide with each other - same convention already used for
email_templates' "one active row per trigger_event" partial unique index.

Purely additive: no existing column altered, no existing row touched.

Revision ID: 08655d0b0117
Revises: b6dda6ad1824
Create Date: 2026-08-10

down_revision updated 2026-08-11: the placeholder merge migration this
chains onto was renamed from 9a1c2f3e6b7d to b6dda6ad1824 after that id
turned out to collide with a teammate's real, unrelated migration - see
b6dda6ad1824_placeholder_for_missing_revision.py's docstring and
docs/known_issues.md's 2026-08-10 entry. This file's own content is
otherwise unchanged.
"""
from alembic import op
import sqlalchemy as sa


revision = "08655d0b0117"
down_revision = "b6dda6ad1824"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaign_candidate_stage_history",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "uq_campaign_candidate_stage_history_idempotency_key",
        "campaign_candidate_stage_history",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_campaign_candidate_stage_history_idempotency_key",
        table_name="campaign_candidate_stage_history",
    )
    op.drop_column("campaign_candidate_stage_history", "idempotency_key")
