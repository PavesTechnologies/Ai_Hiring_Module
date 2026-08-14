"""microsoft_calendar_oauth_tokens

Revision ID: b4122bcc80fe
Revises: 429b9809bc63
Create Date: 2026-08-14 13:41:46.635778

M12 Microsoft Teams calendar integration. Adds `user_oauth_tokens`
(access_token/refresh_token encrypted at rest - BYTEA, one shared
encryption_key_id per row, matching candidates.py's PII-encryption
convention rather than plaintext text columns) and one additive column,
`interview_schedules.external_calendar_event_id`, so reschedule/cancel can
find the right Graph event later.

Letting op.create_table() create the table (and its UNIQUE constraint) in
one call avoids the checkfirst/create_table double-create pitfall hit on
429b9809bc63 - no separately-created enum types here at all, so that
specific failure mode doesn't apply, but the same "let create_table own
its own DDL, don't pre-stage manually" discipline is followed regardless.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b4122bcc80fe'
down_revision: Union[str, Sequence[str], None] = '429b9809bc63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_oauth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(length=255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("access_token_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column("refresh_token_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "encryption_key_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("encryption_keys.id"), nullable=False,
        ),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_oauth_tokens_user_id_provider"),
    )

    op.add_column(
        "interview_schedules",
        sa.Column("external_calendar_event_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interview_schedules", "external_calendar_event_id")
    op.drop_table("user_oauth_tokens")
