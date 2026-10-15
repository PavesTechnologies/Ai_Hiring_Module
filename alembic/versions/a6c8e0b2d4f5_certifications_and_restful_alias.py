"""Certification -> skill mapping; RESTful API aliases (Phase 3 Steps 3 and 5)

- certifications (name, name_variants, note) and certification_skills
  (certification -> catalogue skill, optional sense), edited by HR_ADMIN.
  Seeded: ITIL 4 Foundation, ITIL Foundation (v3 / unversioned) -> Incident
  Management, Change Management (ITSM), Problem Management; ServiceNow CSA ->
  ServiceNow ITSM; ServiceNow CIS-ITSM -> ServiceNow ITSM, Incident
  Management, Change Management (ITSM); PMP -> Program Management. A seed
  skill missing from the catalogue is skipped (printed).
- skill_ontology "REST APIs": aliases "RESTful API", "RESTful APIs" (added
  only if not already there).
- audit: CERTIFICATION_ADDED / _REMOVED, CERTIFICATION_SKILL_ADDED / _REMOVED,
  entity CERTIFICATION.

Revision ID: a6c8e0b2d4f5
Revises: f4b6d8e0a2c3
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6c8e0b2d4f5"
down_revision: Union[str, Sequence[str], None] = "f4b6d8e0a2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ITIL_SKILLS = [("Incident Management", None), ("Change Management", "ITSM"), ("Problem Management", None)]
_SEED = [
    ("ITIL 4 Foundation", ["ITIL 4 Foundation", "ITIL v4 Foundation"], _ITIL_SKILLS),
    ("ITIL Foundation (v3 / unversioned)", ["ITIL Foundation", "ITIL v3 Foundation", "ITIL V3"], _ITIL_SKILLS),
    ("ServiceNow CSA", ["ServiceNow CSA", "ServiceNow Certified System Administrator",
                        "Certified System Administrator CSA"], [("ServiceNow ITSM", None)]),
    ("ServiceNow CIS-ITSM", ["ServiceNow CIS-ITSM", "CIS-ITSM",
                             "Certified Implementation Specialist IT Service Management"],
     [("ServiceNow ITSM", None), ("Incident Management", None), ("Change Management", "ITSM")]),
    ("PMP", ["PMP", "Project Management Professional"], [("Program Management", None)]),
]
_NOTE = "Seeded (Phase 3 Step 5)."


def upgrade() -> None:
    certifications = op.create_table(
        "certifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False, unique=True),
        sa.Column("name_variants", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    certification_skills = op.create_table(
        "certification_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("certification_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("certifications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skill_ontology.id"),
                  nullable=False),
        sa.Column("sense_key", sa.String(50), nullable=True),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("certification_id", "canonical_skill_id",
                            name="uq_certification_skills_certification_skill"),
    )

    bind = op.get_bind()
    skill_ids = dict(bind.execute(sa.text(
        "select canonical_name, id from skill_ontology where is_active and canonical_name = any(:names)"
    ), {"names": sorted({n for _, _, skills in _SEED for n, _ in skills})}).all())
    cert_rows, skill_rows = [], []
    for name, variants, skills in _SEED:
        cert_id = uuid.uuid4()
        cert_rows.append({"id": cert_id, "name": name, "name_variants": variants, "note": _NOTE})
        for skill_name, sense in skills:
            if skill_name not in skill_ids:
                print(f"certification seed: skill '{skill_name}' not in the catalogue - '{name}' -> it skipped")
                continue
            skill_rows.append({"id": uuid.uuid4(), "certification_id": cert_id,
                               "canonical_skill_id": skill_ids[skill_name], "sense_key": sense})
    op.bulk_insert(certifications, cert_rows)
    op.bulk_insert(certification_skills, skill_rows)

    for alias in ("RESTful API", "RESTful APIs"):
        bind.execute(sa.text(
            "update skill_ontology set aliases = array_append(coalesce(aliases, '{}'), :alias) "
            "where canonical_name = 'REST APIs' and not (:alias = any(coalesce(aliases, '{}')))"
        ), {"alias": alias})

    with op.get_context().autocommit_block():
        for value in ("CERTIFICATION_ADDED", "CERTIFICATION_REMOVED",
                      "CERTIFICATION_SKILL_ADDED", "CERTIFICATION_SKILL_REMOVED"):
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")
        op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'CERTIFICATION'")


def downgrade() -> None:
    # Enum values stay (Postgres can't drop them; unused values are harmless).
    op.execute("update skill_ontology set aliases = array_remove(array_remove(aliases, 'RESTful API'), "
               "'RESTful APIs') where canonical_name = 'REST APIs'")
    op.drop_table("certification_skills")
    op.drop_table("certifications")
