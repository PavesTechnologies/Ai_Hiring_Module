"""override_reason_codes: the override reason codes, editable on the Settings page

Revision ID: f6b8d0c2e4a9
Revises: e5a7c9b1d3f8
Create Date: 2026-11-11

Settings phase 1 (approved 2026-11-11). Additive: a new table, filled with the eight codes that were
hard-coded in app/services/campaign/outcome_overrides.py (same codes and labels, same order). Codes are
never renamed or deleted, so every past candidate_outcome_overrides.reason_code keeps its meaning.
Downgrade drops the table; the code then falls back to its built-in list.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6b8d0c2e4a9"
down_revision: Union[str, None] = "e5a7c9b1d3f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CODES = [
    ("SKILL_EVIDENCE_MISSED", "Skill evidence missed"),
    ("SKILL_EVIDENCE_WRONG", "Skill evidence wrong"),
    ("EXPERIENCE_JUDGEMENT", "Experience judgement"),
    ("EDUCATION_EQUIVALENT", "Education equivalent"),
    ("ROLE_FIT", "Role fit"),
    ("HIRING_MANAGER_REQUEST", "Hiring manager request"),
    ("JD_CALIBRATION_WRONG", "JD calibration wrong"),
    ("OTHER", "Other"),
]


def upgrade() -> None:
    table = op.create_table(
        "override_reason_codes",
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("code"),
    )
    op.bulk_insert(table, [{"code": code, "label": label, "is_active": True, "sort_order": (i + 1) * 10}
                           for i, (code, label) in enumerate(_CODES)])


def downgrade() -> None:
    op.drop_table("override_reason_codes")
