"""interview_multi_round_support

Revision ID: 25a1909c01d7
Revises: b4122bcc80fe
Create Date: 2026-08-18 11:20:59.825692

M12 follow-up: multi-round interview scheduling. Drops the
UNIQUE(campaign_candidate_id) constraint on interview_schedules (a
candidate can now have several rounds, one row each), adds round_number,
and replaces it with UNIQUE(campaign_candidate_id, round_number) - a
round number can only exist once per candidate, but the candidate itself
is no longer limited to one row.

round_number DEFAULT 1 for existing rows is safe with nothing to
renumber: this column has never existed before, so every row in this
table today is, by construction, that candidate's only (and therefore
first) round.

Branched off b4122bcc80fe (the actual live head per `alembic current`),
not b4e7c2a91f38 - two unmerged heads currently exist on this shared DB
(see docs/known_issues.md's "multiple unmerged migration heads" entry);
b4e7c2a91f38 is not yet stamped live here, so it isn't the real current
state to branch from.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '25a1909c01d7'
down_revision: Union[str, Sequence[str], None] = 'b4122bcc80fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_UNIQUE = "uq_interview_schedules_campaign_candidate_id"
_NEW_UNIQUE = "uq_interview_schedules_campaign_candidate_id_round_number"


def upgrade() -> None:
    op.drop_constraint(_OLD_UNIQUE, "interview_schedules", type_="unique")
    op.add_column(
        "interview_schedules",
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_unique_constraint(
        _NEW_UNIQUE, "interview_schedules", ["campaign_candidate_id", "round_number"],
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_UNIQUE, "interview_schedules", type_="unique")
    op.drop_column("interview_schedules", "round_number")
    op.create_unique_constraint(_OLD_UNIQUE, "interview_schedules", ["campaign_candidate_id"])
