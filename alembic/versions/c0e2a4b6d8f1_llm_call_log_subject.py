"""llm_call_log.resume_id / campaign_id: what each LLM call was for

Revision ID: c0e2a4b6d8f1
Revises: b8d0f2a4c6e1
Create Date: 2026-10-30

Phase 4 monitoring: LLM calls, tokens and cost per resume and campaign. Both columns are nullable,
filled by new calls only (llm_call_subject), and set to NULL when the resume or campaign is deleted
(erasure keeps the usage counts, not the link). Additive: two nullable columns and an index.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0e2a4b6d8f1"
down_revision: Union[str, Sequence[str], None] = "b8d0f2a4c6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_call_log", sa.Column(
        "resume_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("resumes.id", name="fk_llm_call_log_resume_id", ondelete="SET NULL"), nullable=True))
    op.add_column("llm_call_log", sa.Column(
        "campaign_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("hiring_campaigns.id", name="fk_llm_call_log_campaign_id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_llm_call_log_resume_id", "llm_call_log", ["resume_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_log_resume_id", table_name="llm_call_log")
    op.drop_constraint("fk_llm_call_log_campaign_id", "llm_call_log", type_="foreignkey")
    op.drop_constraint("fk_llm_call_log_resume_id", "llm_call_log", type_="foreignkey")
    op.drop_column("llm_call_log", "campaign_id")
    op.drop_column("llm_call_log", "resume_id")
