"""M11-E03: user_saved_views table + skill-search logging column

Revision ID: c1f4a7b93e20
Revises: 7b3f6a92e1c4
Create Date: 2026-08-07

Branches off 7b3f6a92e1c4 — the revision the target DB is actually stamped at,
read live via alembic rather than assumed from the versions directory, per the
standing workaround in docs/known_issues.md. Apply with an explicit revision id
(`alembic upgrade c1f4a7b93e20`), never a bare `head`.

Adds:
  * user_saved_views  — named filter/sort configurations per user per campaign
    (M11-E03-S03). Server-side rather than browser storage so views follow the
    user across devices and MAX_SAVED_VIEWS_PER_USER is enforceable.
  * search_queries.canonical_skill_ids — the one field M11-E03-S01-T03 needs
    that has no existing equivalent. The spec's `searcher_id`/`searched_at`/
    `zero_results` map onto the existing queried_by/created_at/result_count=0,
    so no further columns are added.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1f4a7b93e20"
down_revision: Union[str, Sequence[str], None] = "7b3f6a92e1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_saved_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["hiring_campaigns.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "campaign_id", "name", name="uq_saved_view_user_campaign_name"),
    )
    op.create_index(
        "ix_user_saved_views_user_campaign", "user_saved_views", ["user_id", "campaign_id"]
    )

    op.add_column(
        "search_queries",
        sa.Column("canonical_skill_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # ── M11-E04-S01 recruiter notes ──────────────────────────────────
    op.create_table(
        "candidate_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["campaign_candidate_id"], ["campaign_candidates.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["deleted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_candidate_notes_cc_id_created_at",
        "candidate_notes",
        ["campaign_candidate_id", "created_at"],
    )

    # ── M11-E05 scheduled exports ─────────────────────────────────────
    # {enabled, frequency, day_of_week, time, top_n, format, recipients[], paused,
    #  last_sent_at}. JSONB rather than nine columns: it is one cohesive config
    # blob that is always read and written whole, and never queried by field.
    op.add_column(
        "hiring_campaigns",
        sa.Column("scheduled_export_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # audit_action_type_enum is a real Postgres enum, so every action label must
    # exist before an audit row using it can be written. IF NOT EXISTS keeps
    # this safe if a teammate's migration adds any of them first.
    for label in (
        "CANDIDATE_NOTE_ADDED", "CANDIDATE_NOTE_UPDATED", "CANDIDATE_NOTE_DELETED",
        "CANDIDATE_LIST_EXPORTED", "SCORECARD_EXPORTED", "SHORTLIST_PACKAGE_EXPORTED",
        "AUDIT_TRAIL_EXPORTED", "COMPLIANCE_REPORT_EXPORTED", "DSAR_EXPORTED",
        "SCHEDULED_EXPORT_CONFIGURED", "SCHEDULED_EXPORT_SENT",
    ):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # Enum labels are deliberately not removed: Postgres has no DROP VALUE, and
    # rebuilding the type would rewrite every audit_log row.
    op.drop_column("hiring_campaigns", "scheduled_export_config")
    op.drop_index("ix_candidate_notes_cc_id_created_at", table_name="candidate_notes")
    op.drop_table("candidate_notes")
    op.drop_column("search_queries", "canonical_skill_ids")
    op.drop_index("ix_user_saved_views_user_campaign", table_name="user_saved_views")
    op.drop_table("user_saved_views")
