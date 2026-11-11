"""email_templates.version and created_by: templates edited on the Settings page are versioned

Revision ID: a7c9e1b3d5f0
Revises: f6b8d0c2e4a9
Create Date: 2026-11-11

Settings phase 1 (approved 2026-11-11). Additive: two columns. Each edit saves a new row (the next version)
and deactivates the old one, which the existing partial unique index already allows; queued emails keep the
version they reference. Existing rows are numbered per trigger event by creation time. Downgrade drops both.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1b3d5f0"
down_revision: Union[str, None] = "f6b8d0c2e4a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_templates", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.add_column("email_templates", sa.Column("created_by", sa.String(length=255), nullable=True))
    op.create_foreign_key("fk_email_templates_created_by", "email_templates", "users", ["created_by"], ["id"])
    op.execute("""
        UPDATE email_templates t SET version = n.rn
        FROM (SELECT id, row_number() OVER (PARTITION BY trigger_event ORDER BY created_at, id) AS rn
              FROM email_templates) n
        WHERE t.id = n.id
    """)


def downgrade() -> None:
    op.drop_constraint("fk_email_templates_created_by", "email_templates", type_="foreignkey")
    op.drop_column("email_templates", "created_by")
    op.drop_column("email_templates", "version")
