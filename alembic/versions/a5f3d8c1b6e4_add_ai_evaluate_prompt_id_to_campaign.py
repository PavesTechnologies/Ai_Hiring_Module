"""add ai_evaluate_prompt_id to hiring_campaigns

Adds an optional (nullable) FK from hiring_campaigns to prompt_templates so
a Campaign can eventually reference its own ACTIVE AI_EVALUATE prompt
template for the AI Evaluation screening stage
(calculate_ai_evaluation_task, app/tasks/ai_evaluation_tasks.py).

Deliberately nullable with no backfill and no NOT NULL tightening, unlike
d3f7b9c2a1e5's prompt_template_id columns - selecting a value on Campaign
create/update is a later phase; this migration only gives the task layer
somewhere to read the value from.

Revision ID: a5f3d8c1b6e4
Revises: d88f97d9d5e0
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a5f3d8c1b6e4"
down_revision = "d88f97d9d5e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hiring_campaigns", sa.Column("ai_evaluate_prompt_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_hiring_campaigns_ai_evaluate_prompt_id",
        "hiring_campaigns", "prompt_templates",
        ["ai_evaluate_prompt_id"], ["id"],
    )
    op.create_index(
        "idx_hiring_campaigns_ai_evaluate_prompt_id", "hiring_campaigns", ["ai_evaluate_prompt_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_hiring_campaigns_ai_evaluate_prompt_id", table_name="hiring_campaigns")
    op.drop_constraint("fk_hiring_campaigns_ai_evaluate_prompt_id", "hiring_campaigns", type_="foreignkey")
    op.drop_column("hiring_campaigns", "ai_evaluate_prompt_id")
