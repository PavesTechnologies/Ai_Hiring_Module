"""scoring_versions: "current vs candidate scoring version" shadow mode (retire v1, phase D)

Revision ID: e1a3c5e7f9b2
Revises: d8f0a2c4e6b9
Create Date: 2026-11-03

Shadow mode stops meaning "v1 vs v2" (v1 no longer runs since phase C) and becomes "the current
scoring version vs a candidate one". A version differs only in configuration - the SCORING_V2_CONFIG
shape and, optionally, the core-confirmation model. Code changes (the prompt included: its version is
a hash of the prompt text) are proved by the golden set before merge, since two code versions can't
run side by side.

Additive:
- scoring_versions (new table):
    id              uuid PK
    label           varchar(100) NOT NULL UNIQUE      - shown in the shadow report, e.g. "v2 baseline"
    configuration   jsonb NOT NULL DEFAULT '{}'       - SCORING_V2_CONFIG shape (merged over the defaults)
    model           varchar(150) NULL                 - core-confirmation model; NULL = the platform's
    status          varchar(20) NOT NULL DEFAULT 'candidate', CHECK in (current, candidate, retired);
                    at most one 'current' (partial unique index)
    note            text NULL
    created_by      varchar(255) NULL FK users.id
    created_at      timestamptz NOT NULL DEFAULT now()
    activated_by    varchar(255) NULL FK users.id     - who promoted it to current
    activated_at    timestamptz NULL
    retired_at      timestamptz NULL
- hiring_campaigns.candidate_scoring_version_id uuid NULL FK scoring_versions.id - when set, each
  candidate is also scored with that version, stored acted_on = false (replaces scoring_v2_shadow;
  that column and scoring_v2_enabled are dropped later, migration 3, with approval).
- candidate_scoring_results.scoring_version_id uuid NULL FK scoring_versions.id (indexed) - which
  version produced a v2 result. Existing rows stay NULL (history: not backfilled).

One row inserted: the 'current' version, label "v2 baseline", whose configuration is the
platform_config SCORING_V2_CONFIG value as it is now (so scoring's configuration - and its config
hash - is unchanged), when that row exists and holds a JSON object. On a fresh database the row
doesn't exist yet at migration time: nothing is inserted, and the seed creates the current version.

Nothing existing is updated or deleted. Dev, 2026-11-03: 1 row inserted; 3 campaigns get a NULL
candidate_scoring_version_id; 758 result rows (379 v1, 379 v2) get a NULL scoring_version_id.

Downgrade drops the two columns and the table (the inserted row with it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1a3c5e7f9b2"
down_revision: Union[str, None] = "d8f0a2c4e6b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scoring_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("label", sa.String(100), nullable=False, unique=True),
        sa.Column("configuration", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("model", sa.String(150), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'candidate'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("activated_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('current', 'candidate', 'retired')", name="ck_scoring_versions_status"),
    )
    op.create_index("uq_scoring_versions_one_current", "scoring_versions", ["status"], unique=True,
                    postgresql_where=sa.text("status = 'current'"))

    op.add_column("hiring_campaigns", sa.Column(
        "candidate_scoring_version_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("scoring_versions.id", name="fk_hiring_campaigns_candidate_scoring_version"), nullable=True))
    op.add_column("candidate_scoring_results", sa.Column(
        "scoring_version_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("scoring_versions.id", name="fk_candidate_scoring_results_scoring_version"), nullable=True))
    op.create_index("ix_candidate_scoring_results_scoring_version_id", "candidate_scoring_results",
                    ["scoring_version_id"])

    # The current version = today's SCORING_V2_CONFIG (only when it holds a JSON object).
    op.execute("""
        insert into scoring_versions (label, configuration, status, note, activated_at)
        select 'v2 baseline', value::jsonb, 'current',
               'Created by migration e1a3c5e7f9b2 from platform_config SCORING_V2_CONFIG.', now()
        from platform_config
        where key = 'SCORING_V2_CONFIG' and value is not null
          and value ~ '^\\s*\\{' and jsonb_typeof(value::jsonb) = 'object'
    """)


def downgrade() -> None:
    op.drop_index("ix_candidate_scoring_results_scoring_version_id", table_name="candidate_scoring_results")
    op.drop_column("candidate_scoring_results", "scoring_version_id")
    op.drop_column("hiring_campaigns", "candidate_scoring_version_id")
    op.drop_index("uq_scoring_versions_one_current", table_name="scoring_versions")
    op.drop_table("scoring_versions")
