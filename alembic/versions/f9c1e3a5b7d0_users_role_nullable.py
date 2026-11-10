"""users.role nullable: a user with no AIRS role has none (deferred finding A-M3)

Revision ID: f9c1e3a5b7d0
Revises: e8b0d2f4a6c8
Create Date: 2026-11-10

Asked for 2026-11-10 (A-M3: unknown users get no AIRS role by default instead of RECRUITER, and the
local role refreshes from the token). The local users row is a shadow of the UMS identity; its role now
mirrors the token's AIRS role on every AIRS request, and is NULL when the token has none. Only relaxes
NOT NULL; existing rows are not rewritten here - each is corrected on its user's next request.

Downgrade sets NOT NULL again; it fails while any user has no role.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f9c1e3a5b7d0"
down_revision: Union[str, None] = "e8b0d2f4a6c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE = sa.Enum("HR_ADMIN", "RECRUITER", "HIRING_MANAGER", name="user_role_enum", create_type=False)


def upgrade() -> None:
    op.alter_column("users", "role", existing_type=_ROLE, nullable=True)


def downgrade() -> None:
    op.alter_column("users", "role", existing_type=_ROLE, nullable=False)
