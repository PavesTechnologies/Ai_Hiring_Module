"""delete the v1-only scoring settings from platform_config (retire v1, phase E - migration 4)

Revision ID: a3c5e7f9b2d4
Revises: f2b4d6e8a0c1
Create Date: 2026-11-03

v1 scoring was removed in phase E step 2 (111f1d5); scoring reads the current scoring version only since
phase E part 1 (6be3c60). Nothing in the backend or the airs frontend reads these keys any more:

  DETERMINISTIC_WEIGHT_SKILLS, DETERMINISTIC_WEIGHT_EXPERIENCE, DETERMINISTIC_WEIGHT_EDUCATION,
  DETERMINISTIC_WEIGHT_FUNCTIONAL, CORE_IMPORTANCE_WEIGHT_MULTIPLIER, SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER,
  HIERARCHY_GRANDCHILD_MULTIPLIER, HIERARCHY_SEMANTIC_ONLY_THRESHOLD, HIERARCHY_CHILD_MULTIPLIER,
  HIERARCHY_SIBLING_MULTIPLIER, SEMANTIC_MULTIPLIER, PREFERRED_SKILL_SHARE, SCORING_V2_CONFIG, SCORING_V2_SHADOW

Each row present is copied first to platform_config_backup (new table: key PK, value, description,
updated_at, updated_by, reason, saved_at), then deleted. Downgrade puts them back from it and drops the
table. Dropping the backup table later needs approval.

Dev, 2026-11-03: 6 rows - DETERMINISTIC_WEIGHT_SKILLS 0.70, DETERMINISTIC_WEIGHT_EXPERIENCE 0.15,
DETERMINISTIC_WEIGHT_EDUCATION 0.15, CORE_IMPORTANCE_WEIGHT_MULTIPLIER 1.0,
SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER 1.0, SCORING_V2_CONFIG (503 chars, equal to the current scoring
version's configuration). The other 8 keys have no row. A fresh database has none of them once the seeds
stop creating them (phase E).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b2d4"
down_revision: Union[str, None] = "f2b4d6e8a0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

V1_KEYS = (
    "DETERMINISTIC_WEIGHT_SKILLS", "DETERMINISTIC_WEIGHT_EXPERIENCE", "DETERMINISTIC_WEIGHT_EDUCATION",
    "DETERMINISTIC_WEIGHT_FUNCTIONAL", "CORE_IMPORTANCE_WEIGHT_MULTIPLIER", "SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER",
    "HIERARCHY_GRANDCHILD_MULTIPLIER", "HIERARCHY_SEMANTIC_ONLY_THRESHOLD", "HIERARCHY_CHILD_MULTIPLIER",
    "HIERARCHY_SIBLING_MULTIPLIER", "SEMANTIC_MULTIPLIER", "PREFERRED_SKILL_SHARE", "SCORING_V2_CONFIG",
    "SCORING_V2_SHADOW",
)
REASON = "retire v1, phase E: v1-only scoring setting"


def upgrade() -> None:
    op.create_table(
        "platform_config_backup",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    keys = sa.bindparam("keys", value=list(V1_KEYS), expanding=True)
    op.get_bind().execute(sa.text(
        "insert into platform_config_backup (key, value, description, updated_at, updated_by, reason) "
        "select key, value, description, updated_at, updated_by, :reason from platform_config where key in :keys"
    ).bindparams(keys, reason=REASON))
    op.get_bind().execute(sa.text("delete from platform_config where key in :keys").bindparams(keys))


def downgrade() -> None:
    op.execute("""
        insert into platform_config (id, key, value, description, updated_at, updated_by)
        select gen_random_uuid(), b.key, b.value, b.description, coalesce(b.updated_at, now()), b.updated_by
        from platform_config_backup b
        where not exists (select 1 from platform_config c where c.key = b.key)
    """)
    op.drop_table("platform_config_backup")
