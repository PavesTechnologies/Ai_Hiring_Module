"""Education field families per role domain (Phase 3 Step 2)

- education_field_relations: two fields of study that are one family for JDs
  of a role domain (unordered pair, stored field_a < field_b), edited by
  HR_ADMIN. Seeded for SOFTWARE: CS-IT, CS-ECE, CS-SE, IT-SE,
  CS-Computer Applications, IT-Computer Applications.
- job_descriptions.role_domain: set in JD calibration (SOFTWARE /
  IT_OPERATIONS / DATA / BUSINESS / OTHER); NULL = not set.
- audit: EDUCATION_FIELD_RELATION_ADDED / _REMOVED, entity EDUCATION_FIELD_RELATION.

Revision ID: f4b6d8e0a2c3
Revises: e3a5c7d9f1b2
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b6d8e0a2c3"
down_revision: Union[str, Sequence[str], None] = "e3a5c7d9f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_DOMAINS = "('SOFTWARE', 'IT_OPERATIONS', 'DATA', 'BUSINESS', 'OTHER')"
_SOFTWARE_PAIRS = [
    ("COMPUTER_SCIENCE", "INFORMATION_TECHNOLOGY"),
    ("COMPUTER_SCIENCE", "ELECTRONICS_ENGINEERING"),
    ("COMPUTER_SCIENCE", "SOFTWARE_ENGINEERING"),
    ("INFORMATION_TECHNOLOGY", "SOFTWARE_ENGINEERING"),
    ("COMPUTER_APPLICATIONS", "COMPUTER_SCIENCE"),
    ("COMPUTER_APPLICATIONS", "INFORMATION_TECHNOLOGY"),
]


def upgrade() -> None:
    relations = op.create_table(
        "education_field_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("field_a", sa.String(40), nullable=False),
        sa.Column("field_b", sa.String(40), nullable=False),
        sa.Column("role_domain", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("field_a", "field_b", "role_domain", name="uq_education_field_relations_pair_domain"),
        sa.CheckConstraint("field_a < field_b", name="ck_education_field_relations_ordered"),
        sa.CheckConstraint(f"role_domain IN {_ROLE_DOMAINS}", name="ck_education_field_relations_role_domain"),
    )
    op.bulk_insert(relations, [
        {"id": uuid.uuid4(), "field_a": a, "field_b": b, "role_domain": "SOFTWARE",
         "note": "Seeded (Phase 3 Step 2): one family for software roles."}
        for a, b in _SOFTWARE_PAIRS
    ])

    op.add_column("job_descriptions", sa.Column("role_domain", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_job_descriptions_role_domain", "job_descriptions",
        f"role_domain IS NULL OR role_domain IN {_ROLE_DOMAINS}",
    )

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'EDUCATION_FIELD_RELATION_ADDED'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'EDUCATION_FIELD_RELATION_REMOVED'")
        op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'EDUCATION_FIELD_RELATION'")


def downgrade() -> None:
    # Enum values stay (Postgres can't drop them; unused values are harmless).
    op.drop_constraint("ck_job_descriptions_role_domain", "job_descriptions", type_="check")
    op.drop_column("job_descriptions", "role_domain")
    op.drop_table("education_field_relations")
