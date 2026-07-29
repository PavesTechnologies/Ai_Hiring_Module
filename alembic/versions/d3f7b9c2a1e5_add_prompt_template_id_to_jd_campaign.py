"""add prompt_template_id to job_descriptions and hiring_campaigns

Mandatory Prompt Template selection: every Job Description must reference
an ACTIVE JD_PARSE prompt_templates row, and every Hiring Campaign must
reference an ACTIVE RESUME_PARSE prompt_templates row.

Both tables may already have rows (created before this feature existed),
so the column is added nullable, backfilled with the oldest matching
ACTIVE prompt template, then tightened to NOT NULL - same pattern as
a1b2c3d4e5f6_jd_required_metadata_fields.py. If no matching ACTIVE
prompt template exists yet for a table with existing rows, the backfill
UPDATE is a no-op and the subsequent NOT NULL alter fails loudly rather
than silently leaving orphaned rows - a matching prompt template must be
created first (see app.seeds / POST /airs/prompt-templates).

Revision ID: d3f7b9c2a1e5
Revises: c9e2a5f8b1d4
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d3f7b9c2a1e5"
down_revision = "c9e2a5f8b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_descriptions", sa.Column("prompt_template_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "hiring_campaigns", sa.Column("prompt_template_id", postgresql.UUID(as_uuid=True), nullable=True)
    )

    op.execute("""
        UPDATE job_descriptions
        SET prompt_template_id = (
            SELECT id FROM prompt_templates
            WHERE task_type = 'JD_PARSE' AND status = 'ACTIVE'
            ORDER BY created_at ASC
            LIMIT 1
        )
        WHERE prompt_template_id IS NULL
    """)
    op.execute("""
        UPDATE hiring_campaigns
        SET prompt_template_id = (
            SELECT id FROM prompt_templates
            WHERE task_type = 'RESUME_PARSE' AND status = 'ACTIVE'
            ORDER BY created_at ASC
            LIMIT 1
        )
        WHERE prompt_template_id IS NULL
    """)

    op.alter_column("job_descriptions", "prompt_template_id", nullable=False)
    op.alter_column("hiring_campaigns", "prompt_template_id", nullable=False)

    op.create_foreign_key(
        "fk_job_descriptions_prompt_template_id",
        "job_descriptions", "prompt_templates",
        ["prompt_template_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_hiring_campaigns_prompt_template_id",
        "hiring_campaigns", "prompt_templates",
        ["prompt_template_id"], ["id"],
    )
    op.create_index(
        "idx_job_descriptions_prompt_template_id", "job_descriptions", ["prompt_template_id"]
    )
    op.create_index(
        "idx_hiring_campaigns_prompt_template_id", "hiring_campaigns", ["prompt_template_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_hiring_campaigns_prompt_template_id", table_name="hiring_campaigns")
    op.drop_index("idx_job_descriptions_prompt_template_id", table_name="job_descriptions")
    op.drop_constraint("fk_hiring_campaigns_prompt_template_id", "hiring_campaigns", type_="foreignkey")
    op.drop_constraint("fk_job_descriptions_prompt_template_id", "job_descriptions", type_="foreignkey")
    op.drop_column("hiring_campaigns", "prompt_template_id")
    op.drop_column("job_descriptions", "prompt_template_id")
