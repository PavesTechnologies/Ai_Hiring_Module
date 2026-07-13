"""jd_job_id_notice_period_verification

Revision ID: b3d8f1a02c77
Revises: f6a1c9e34d02
Create Date: 2026-07-11 00:00:00.000000

Renames job_descriptions.parsed_skills to extracted_json (the column always
held the full raw AI-extraction payload; required_skills already carries the
normalized required/preferred lists derived from it). Also adds:
  - job_id: human-readable sequential identifier ("JOB_1", "JOB_2", ...),
    backed by a DB sequence so concurrent inserts never collide.
  - notice_period: integer, business-supplied, not populated by the pipeline.
  - is_verified: PARTIALLY_VERIFIED once the JD pipeline finishes all stages,
    upgraded to VERIFIED once the JD has no unknown skills.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3d8f1a02c77'
down_revision: Union[str, Sequence[str], None] = 'f6a1c9e34d02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOB_ID_SEQ = "job_descriptions_job_id_seq"


def upgrade() -> None:
    """Upgrade schema."""

    op.alter_column('job_descriptions', 'parsed_skills', new_column_name='extracted_json')

    op.execute(f"CREATE SEQUENCE {JOB_ID_SEQ}")

    op.add_column('job_descriptions', sa.Column('job_id', sa.String(length=50), nullable=True))
    op.execute(
        f"UPDATE job_descriptions SET job_id = 'JOB_' || nextval('{JOB_ID_SEQ}') WHERE job_id IS NULL"
    )
    op.alter_column(
        'job_descriptions', 'job_id',
        nullable=False,
        server_default=sa.text(f"'JOB_' || nextval('{JOB_ID_SEQ}')"),
    )
    op.create_unique_constraint('uq_job_descriptions_job_id', 'job_descriptions', ['job_id'])

    op.add_column('job_descriptions', sa.Column('notice_period', sa.Integer(), nullable=True))

    jd_verification_status_enum = sa.Enum(
        'NOT_VERIFIED', 'PARTIALLY_VERIFIED', 'VERIFIED',
        name='jd_verification_status_enum',
    )
    jd_verification_status_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'job_descriptions',
        sa.Column(
            'is_verified', jd_verification_status_enum,
            nullable=False, server_default='NOT_VERIFIED',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_column('job_descriptions', 'is_verified')
    sa.Enum(name='jd_verification_status_enum').drop(op.get_bind(), checkfirst=True)

    op.drop_column('job_descriptions', 'notice_period')

    op.drop_constraint('uq_job_descriptions_job_id', 'job_descriptions', type_='unique')
    op.drop_column('job_descriptions', 'job_id')
    op.execute(f"DROP SEQUENCE IF EXISTS {JOB_ID_SEQ}")

    op.alter_column('job_descriptions', 'extracted_json', new_column_name='parsed_skills')
