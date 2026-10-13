"""JD unknown-skill resolution + provisional scoring (Phase 2 Step 2)

- jd_unknown_skills: how HR resolved this JD's unknown skill - resolution
  (MAPPED / NEW_SKILL / DROPPED), resolved_canonical_skill_id, resolved_by,
  resolved_at. A resolved link keeps status RESOLVED; DROPPED creates no
  jd_skills row.
- resume_provisional_skill_scans: per resume, which unresolved JD unknown
  skills the v2 detection scanned the redacted resume text for, and the
  hits (scanner rules: short terms, cue words, spelling). The text itself
  is never stored, so this is what v2 provisional scoring reads.
- audit action JD_UNKNOWN_SKILL_RESOLVED.

Revision ID: d2f4a6b8c0e1
Revises: c7e9a3f5b1d2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2f4a6b8c0e1"
down_revision: Union[str, Sequence[str], None] = "c7e9a3f5b1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    resolution = postgresql.ENUM("MAPPED", "NEW_SKILL", "DROPPED", name="jd_unknown_skill_resolution_enum")
    resolution.create(op.get_bind(), checkfirst=True)

    op.add_column("jd_unknown_skills", sa.Column(
        "resolution", postgresql.ENUM(name="jd_unknown_skill_resolution_enum", create_type=False), nullable=True))
    op.add_column("jd_unknown_skills", sa.Column(
        "resolved_canonical_skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skill_ontology.id"), nullable=True))
    op.add_column("jd_unknown_skills", sa.Column("resolved_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("jd_unknown_skills", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "resume_provisional_skill_scans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("resume_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("resumes.id"), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("detection_version", sa.String(20), nullable=False),
        sa.Column("scanned_unknown_skill_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("hits", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("resume_id", name="uq_resume_provisional_skill_scans_resume"),
    )

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'JD_UNKNOWN_SKILL_RESOLVED'")


def downgrade() -> None:
    op.drop_table("resume_provisional_skill_scans")
    for column in ("resolved_at", "resolved_by", "resolved_canonical_skill_id", "resolution"):
        op.drop_column("jd_unknown_skills", column)
    postgresql.ENUM(name="jd_unknown_skill_resolution_enum").drop(op.get_bind(), checkfirst=True)
    # The audit enum value can't be dropped in PostgreSQL; left in place.
