"""users.airs_roles: every AIRS role the token holds

Revision ID: e5a7c9b1d3f8
Revises: d4f6b8c0e2a5
Create Date: 2026-11-11

Settings phase 1 (approved 2026-11-11: alert recipients are all active HR admins, secondary-role HR admins
included). users.role keeps only the token's first AIRS role, so an HR admin whose token lists another role
first got no HR-admin alerts. Additive: a new column, filled from users.role now and from the full token on
each user's next request. Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a7c9b1d3f8"
down_revision: Union[str, None] = "d4f6b8c0e2a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("airs_roles", postgresql.ARRAY(sa.String(length=32)), nullable=False,
                                     server_default=sa.text("'{}'")))
    op.execute("UPDATE users SET airs_roles = ARRAY[role::text] WHERE role IS NOT NULL")


def downgrade() -> None:
    op.drop_column("users", "airs_roles")
