"""merge current heads for jd retry checkpoint support

Revision ID: c71678d36109
Revises: a92163422dba, 793ec14a7a28, c8f2a4d6e910
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c71678d36109'
down_revision: Union[str, Sequence[str], None] = ('a92163422dba', '793ec14a7a28', 'c8f2a4d6e910')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
