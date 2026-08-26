"""interview feedback requested trigger event

Epic 5 Step 4 - adds INTERVIEW_FEEDBACK_REQUESTED to
email_trigger_event_enum. None of the 6 existing values fit "please
submit interview feedback" (confirmed via Step 0's investigation) - this
is the 7th real trigger event, and the first one whose real recipient is
an EXTERNAL_INTERVIEWER row rather than a candidate.

Schema only - the actual [SEED]-tagged email_templates row is inserted
by seed_email_templates.py (matching every prior trigger event's own
convention: migrations own the enum/schema, the seed script owns
template content).

Branched off b1d363dbbdb3 (the actual live head per `alembic current`),
not b4e7c2a91f38 - same standing multiple-unmerged-heads situation
documented in docs/known_issues.md.

Revision ID: c03d249037ca
Revises: b1d363dbbdb3
Create Date: 2026-08-18
"""
from alembic import op

revision = "c03d249037ca"
down_revision = "b1d363dbbdb3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_FEEDBACK_REQUESTED'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value in place - removing this
    # value would require rebuilding the type (same limitation every
    # other ADD VALUE migration in this codebase already accepts).
    pass
