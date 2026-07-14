"""jd required metadata fields: max_experience_years + NOT NULL

Adds max_experience_years (mirrors min_experience_years) and tightens
extracted_json/required_skills/min_experience_years/notice_period/
education_criteria to NOT NULL on job_descriptions - these four are now
required at the API layer (CreateJDRequest/UpdateJDRequest), so the
column constraints should match.

Revision ID: a1b2c3d4e5f6
Revises: d5c1a0b2e3f4
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "d5c1a0b2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_descriptions",
        sa.Column("max_experience_years", sa.Numeric(precision=4, scale=1), nullable=True),
    )

    # Backfill any pre-existing rows before tightening these columns to
    # NOT NULL - they were nullable when those rows were created.
    op.execute("UPDATE job_descriptions SET max_experience_years = 0 WHERE max_experience_years IS NULL")
    op.execute("UPDATE job_descriptions SET extracted_json = '{}'::jsonb WHERE extracted_json IS NULL")
    op.execute("UPDATE job_descriptions SET required_skills = '{}'::jsonb WHERE required_skills IS NULL")
    op.execute("UPDATE job_descriptions SET min_experience_years = 0 WHERE min_experience_years IS NULL")
    op.execute("UPDATE job_descriptions SET notice_period = 0 WHERE notice_period IS NULL")
    op.execute("UPDATE job_descriptions SET education_criteria = '{}'::jsonb WHERE education_criteria IS NULL")

    op.alter_column("job_descriptions", "max_experience_years", nullable=False)
    op.alter_column("job_descriptions", "extracted_json", nullable=False)
    op.alter_column("job_descriptions", "required_skills", nullable=False)
    op.alter_column("job_descriptions", "min_experience_years", nullable=False)
    op.alter_column("job_descriptions", "notice_period", nullable=False)
    op.alter_column("job_descriptions", "education_criteria", nullable=False)


def downgrade() -> None:
    op.alter_column("job_descriptions", "education_criteria", nullable=True)
    op.alter_column("job_descriptions", "notice_period", nullable=True)
    op.alter_column("job_descriptions", "min_experience_years", nullable=True)
    op.alter_column("job_descriptions", "required_skills", nullable=True)
    op.alter_column("job_descriptions", "extracted_json", nullable=True)
    op.drop_column("job_descriptions", "max_experience_years")
