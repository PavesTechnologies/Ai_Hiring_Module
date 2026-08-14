"""placeholder for missing revision (scheduled export / compliance drift)

Discovered 2026-08-13 while verifying the c1f4a7b93e20/7b3f6a92e1c4 merge
above was safe to stamp - a full recursive model-vs-live drift audit
(tables/columns/enums) found further drift completely unrelated to that
merge, deliberately NOT folded into it (see docs/known_issues.md - this
and the c1f4a7b93e20 merge have unrelated root causes and are documented
as two separate entries so a future reader isn't left guessing which
finding belongs to which incident):

  - hiring_campaigns.scheduled_export_config (jsonb, nullable)
  - audit_action_type_enum: 8 new live values with zero Python/model
    presence anywhere in this checkout - AUDIT_TRAIL_EXPORTED,
    CANDIDATE_LIST_EXPORTED, COMPLIANCE_REPORT_EXPORTED, DSAR_EXPORTED,
    SCHEDULED_EXPORT_CONFIGURED, SCHEDULED_EXPORT_SENT, SCORECARD_EXPORTED,
    SHORTLIST_PACKAGE_EXPORTED

Searched exhaustively before concluding this is genuinely missing, not
another c1f4a7b93e20-style "hasn't been pulled yet" situation: `git fetch
--all` (picked up fresh commits on RMe/loki/main/niharika), then a
per-branch `git grep` sanity-checked against a term known to exist
(c1f4a7b93e20 on origin/main, confirmed found) before searching every
local and remote branch tip for scheduled_export_config/DSAR_EXPORTED -
zero hits anywhere, local or remote, current or historical
(`git log --all --diff-filter=A`). Reads like one cohesive, unbuilt
"compliance / scheduled exports" feature (DSAR = Data Subject Access
Request) - column + enum values are thematically consistent with each
other, applied directly to this shared dev DB with no corresponding
migration file or application code anywhere.

Authored as a no-op merge into the exact stamped revision id, same
conservative methodology as every prior recurrence in this file - does
NOT attempt to recreate the original DDL (unknown: nullability already
confirmed nullable, but default value, any check constraint, or index are
not), and does NOT add the corresponding model/service code (out of
scope - that's a real feature to build, not a schema-bookkeeping fix).

Revision ID: e9961d228f3d
Revises: 43535e9e3cf7
Create Date: 2026-08-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9961d228f3d'
down_revision: Union[str, Sequence[str], None] = '43535e9e3cf7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
