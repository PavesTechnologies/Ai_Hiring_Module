"""Profile template: listed-only skills can be confirmed by vote

- jd_profile_templates.listed_only_can_confirm: whether a core skill whose
  only evidence is a skills-list line (or lines where the candidate sells /
  demos / supports / attends) can be confirmed by the 3-vote check; off =
  at most UNCERTAIN. On for FRESHER, off for EXPERIENCED and DOMAIN.
  Edited by HR_ADMIN with the other template settings (audited).

Revision ID: e2a4c6e8f0b3
Revises: d1f3a5c7e9b2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6e8f0b3"
down_revision: Union[str, Sequence[str], None] = "d1f3a5c7e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jd_profile_templates", sa.Column("listed_only_can_confirm", sa.Boolean(), nullable=False,
                                                    server_default=sa.false()))
    op.execute("UPDATE jd_profile_templates SET listed_only_can_confirm = true WHERE key = 'FRESHER'")


def downgrade() -> None:
    op.drop_column("jd_profile_templates", "listed_only_can_confirm")
