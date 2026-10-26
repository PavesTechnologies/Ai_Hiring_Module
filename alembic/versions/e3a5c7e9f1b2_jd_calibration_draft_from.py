"""job_descriptions.calibration_draft_from: the version whose calibration a new version carries

Revision ID: e3a5c7e9f1b2
Revises: d2f4a6c8e0b1
Create Date: 2026-10-26

A new JD version (a details edit, or new text / a new file) carries the previous
version's calibration forward as a draft for HR to re-confirm, instead of starting
empty; this records which version it came from. Additive: a new nullable column,
no existing value changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a5c7e9f1b2"
down_revision: Union[str, Sequence[str], None] = "d2f4a6c8e0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_descriptions", sa.Column(
        "calibration_draft_from", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("job_descriptions.id", name="fk_job_descriptions_calibration_draft_from"), nullable=True))


def downgrade() -> None:
    op.drop_constraint("fk_job_descriptions_calibration_draft_from", "job_descriptions", type_="foreignkey")
    op.drop_column("job_descriptions", "calibration_draft_from")
