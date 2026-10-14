"""JD calibration: internship credit (Phase 3 Step 1)

- job_descriptions.internship_credit_pct: how much internship time counts as
  experience - 100 / 50 / 0 (%). NULL = the default: 100 on a fresher JD
  (calibrated minimum <= 1 year), else 0. Existing JDs stay NULL.

Revision ID: e3a5c7d9f1b2
Revises: d2f4a6b8c0e1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a5c7d9f1b2"
down_revision: Union[str, Sequence[str], None] = "d2f4a6b8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_descriptions", sa.Column("internship_credit_pct", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_job_descriptions_internship_credit_pct",
        "job_descriptions",
        "internship_credit_pct IS NULL OR internship_credit_pct IN (0, 50, 100)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_descriptions_internship_credit_pct", "job_descriptions", type_="check")
    op.drop_column("job_descriptions", "internship_credit_pct")
