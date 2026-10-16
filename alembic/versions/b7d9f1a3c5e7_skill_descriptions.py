"""Skill descriptions for paraphrase search (Phase 3 Step 6)

- skill_ontology.description: a one-line description of what the skill means,
  compared with resume lines by the scoring v2 paraphrase search. Optional,
  edited by HR in the Skill form.
- Seeded for the core skills of the three dev JDs (11 skills). A skill
  missing from the catalogue is skipped (printed); an existing description
  is never overwritten.
- Change Management's ITSM sense description gains the ITSM change wording
  paraphrase search needs (change requests, RFC, CAB, release approvals,
  change calendar) - in the sense, not the skill, so an organisational-change
  JD isn't pulled towards ITSM lines.

Revision ID: b7d9f1a3c5e7
Revises: a6c8e0b2d4f5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9f1a3c5e7"
down_revision: Union[str, Sequence[str], None] = "a6c8e0b2d4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DESCRIPTIONS = {
    "ServiceNow ITSM": "Configuring, administering or implementing ServiceNow's IT Service Management modules "
                       "(incident, change, problem, request) - not only using ServiceNow to raise or log tickets.",
    "Incident Management": "Handling IT incidents and outages: logging, prioritising, resolving and restoring "
                           "service, incident workflows and major incidents.",
    "Service Catalog": "Building and maintaining ServiceNow Service Catalog items, request forms, catalogue "
                       "variables and request fulfilment workflows: request portal, request items, catalogue items, "
                       "request fulfilment.",
    "Change Management": "Managing change.",
    "Python": "Programming in Python: writing scripts, applications or services in the Python language.",
    "REST APIs": "Designing, building or integrating RESTful HTTP APIs and web service endpoints.",
    "git": "Version control with Git: branches, commits, pull requests and code review workflows.",
    "sql": "Writing SQL queries against relational databases: selects, joins, aggregations and schemas.",
    "AI Governance": "Governing the use of AI: AI policies, risk, ethics, compliance and responsible-AI oversight.",
    "AI Transformation": "Leading the adoption of AI across a business: AI strategy, use cases and organisation-wide "
                         "AI programmes.",
    "Program Management": "Running programmes of related projects: planning, governance, budgets, dependencies "
                          "and stakeholder reporting.",
}


_ITSM_CHANGE_WORDS = " Change requests, RFC, CAB, release approvals, change calendar."


def upgrade() -> None:
    op.add_column("skill_ontology", sa.Column("description", sa.Text(), nullable=True))
    bind = op.get_bind()
    for name, description in _DESCRIPTIONS.items():
        updated = bind.execute(sa.text(
            "update skill_ontology set description = :d where canonical_name = :n and description is null"
        ), {"d": description, "n": name}).rowcount
        if not updated:
            print(f"skill description seed: '{name}' not in the catalogue (or already described) - skipped")
    updated = bind.execute(sa.text(
        "update skill_senses set description = description || :w where sense_key = 'ITSM' "
        "and description not like '%RFC%' and canonical_skill_id = "
        "(select id from skill_ontology where canonical_name = 'Change Management')"
    ), {"w": _ITSM_CHANGE_WORDS}).rowcount
    if not updated:
        print("Change Management ITSM sense: not found (or already has the wording) - skipped")


def downgrade() -> None:
    op.execute(sa.text("update skill_senses set description = replace(description, :w, '') where sense_key = 'ITSM'")
               .bindparams(w=_ITSM_CHANGE_WORDS))
    op.drop_column("skill_ontology", "description")
