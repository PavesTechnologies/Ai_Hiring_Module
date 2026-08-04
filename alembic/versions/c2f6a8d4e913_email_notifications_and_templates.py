"""add email_templates and email_notifications tables

M07-E03 S02 T02/T03: no email infrastructure existed anywhere in this
codebase before this - creates the two tables + two enums needed to queue
and track candidate-rejection emails. email_notifications never stores a
plaintext email address (only candidate_id - decrypted at send time).

Revision ID: c2f6a8d4e913
Revises: b1e4f7c92a05
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c2f6a8d4e913"
down_revision = "b1e4f7c92a05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    email_trigger_event_enum = postgresql.ENUM(
        "CANDIDATE_REJECTED", name="email_trigger_event_enum", create_type=False,
    )
    email_notification_status_enum = postgresql.ENUM(
        "QUEUED", "SENT", "FAILED", name="email_notification_status_enum", create_type=False,
    )
    email_trigger_event_enum.create(op.get_bind(), checkfirst=True)
    email_notification_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "email_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_event", email_trigger_event_enum, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("email_templates_pkey")),
    )
    op.create_index(
        "uq_email_templates_active_trigger_event",
        "email_templates",
        ["trigger_event"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "email_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_event", email_trigger_event_enum, nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", email_notification_status_enum, nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], name=op.f("email_notifications_candidate_id_fkey")),
        sa.ForeignKeyConstraint(
            ["campaign_candidate_id"], ["campaign_candidates.id"],
            name=op.f("email_notifications_campaign_candidate_id_fkey"),
        ),
        sa.ForeignKeyConstraint(["template_id"], ["email_templates.id"], name=op.f("email_notifications_template_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("email_notifications_pkey")),
    )


def downgrade() -> None:
    op.drop_table("email_notifications")
    op.drop_index("uq_email_templates_active_trigger_event", table_name="email_templates")
    op.drop_table("email_templates")
    postgresql.ENUM(name="email_notification_status_enum").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="email_trigger_event_enum").drop(op.get_bind(), checkfirst=True)
