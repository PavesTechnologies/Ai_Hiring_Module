"""Recruiter notes table + skill-search logging column + audit enum labels

Revision ID: c1f4a7b93e20
Revises: 7b3f6a92e1c4
Create Date: 2026-08-07

Branches off 7b3f6a92e1c4 — the revision the target DB is actually stamped at,
read live via alembic rather than assumed from the versions directory, per the
standing workaround in docs/known_issues.md. Apply with an explicit revision id
(`alembic upgrade c1f4a7b93e20`), never a bare `head`.

Adds:
  * candidate_notes — free-text recruiter notes per candidate per campaign.
    A table rather than a column because notes are many-per-candidate,
    individually authored, editable and soft-deletable.
  * search_queries.canonical_skill_ids — the one field skill-search analytics
    needs that has no existing equivalent. `searcher_id`/`searched_at`/
    `zero_results` map onto the existing queried_by/created_at/result_count=0,
    so no further columns are added.
  * audit_action_type_enum labels for the note and export actions. That enum is
    a real Postgres type, so a label must exist before an audit row can use it.
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

    # audit_action_type_enum is a real Postgres enum, so every action label must
    # exist before an audit row using it can be written. IF NOT EXISTS keeps
    # this safe if a teammate's migration adds any of them first.
    for label in (
        "CANDIDATE_NOTE_ADDED", "CANDIDATE_NOTE_UPDATED", "CANDIDATE_NOTE_DELETED",
        "CANDIDATE_LIST_EXPORTED", "SCORECARD_EXPORTED", "SHORTLIST_PACKAGE_EXPORTED",
        "AUDIT_TRAIL_EXPORTED", "COMPLIANCE_REPORT_EXPORTED",
    ):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # Enum labels are deliberately not removed: Postgres has no DROP VALUE, and
    # rebuilding the type would rewrite every audit_log row.
    op.drop_index("ix_candidate_notes_cc_id_created_at", table_name="candidate_notes")
    op.drop_table("candidate_notes")
    op.drop_column("search_queries", "canonical_skill_ids")
