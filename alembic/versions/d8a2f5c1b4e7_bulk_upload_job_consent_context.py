"""bulk upload job consent context (ip_address, user_agent, jurisdiction)

Captures the uploader's IP/user-agent and declared jurisdiction once per
bulk_upload_jobs row (one ZIP submission = one consent event), so every
candidate created from a file inside that ZIP can carry real consent
context instead of NULL/hardcoded GLOBAL.

Revision ID: d8a2f5c1b4e7
Revises: 2c82aaa93c9f
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "d8a2f5c1b4e7"
down_revision = "2c82aaa93c9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bulk_upload_jobs", sa.Column("ip_address", sa.String(length=45), nullable=True))
    op.add_column("bulk_upload_jobs", sa.Column("user_agent", sa.Text(), nullable=True))
    op.add_column(
        "bulk_upload_jobs",
        sa.Column("jurisdiction", sa.String(length=10), nullable=False, server_default="GLOBAL"),
    )


def downgrade() -> None:
    op.drop_column("bulk_upload_jobs", "jurisdiction")
    op.drop_column("bulk_upload_jobs", "user_agent")
    op.drop_column("bulk_upload_jobs", "ip_address")
