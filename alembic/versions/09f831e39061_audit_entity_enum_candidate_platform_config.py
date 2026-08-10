"""audit entity enum: candidate + platform_config

EntityType.CANDIDATE and EntityType.PLATFORM_CONFIG (app/enums/constants.py)
were never added to the live Postgres audit_entity_type_enum type - e.g.
CandidateErasureService.erase_candidate's own audit_service.log(entity_type=
EntityType.CANDIDATE) was failing with psycopg2.errors.InvalidTextRepresentation
(discovered while auditing DELETE /candidates/{candidate_id}'s 500).

ALTER TYPE ... ADD VALUE cannot run inside a transaction block - same
transactional_ddl = False precedent as every other audit-enum migration in
this project (see 5439e70a5a8e, d88f97d9d5e0, f7c1a4d8b3e6).

Revision ID: 09f831e39061
Revises: 7043b9ed5abe
Create Date: 2026-08-06

Re-parented 2026-08-07 onto the squashed initial schema (7043b9ed5abe) after
its original parent (5439e70a5a8e) was removed in that squash - both values
this migration adds are already present in the squashed initial schema's
audit_entity_type_enum definition, so this is now a harmless no-op kept for
historical record rather than deleted outright.
"""
from alembic import op

revision = "09f831e39061"
down_revision = "7043b9ed5abe"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'CANDIDATE'")
    op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'PLATFORM_CONFIG'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
