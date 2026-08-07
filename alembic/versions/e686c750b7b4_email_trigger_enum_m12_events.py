"""email trigger enum M12 interview/selection events

Adds the four email_trigger_event_enum values M12 (Workflow & Interview
Scheduling) needs: INTERVIEW_SCHEDULED, INTERVIEW_RESCHEDULED,
INTERVIEW_CANCELLED, CANDIDATE_SELECTED. CANDIDATE_REJECTED and
UPLOAD_PERMANENTLY_FAILED already exist and are untouched.

Deliberately schema-only - no email_templates rows are inserted here. A
follow-up migration/seed adds those once these values are guaranteed
committed and visible (Postgres will not let the same transaction that adds
an enum value also use it - see below), matching the same split already used
for d6b8e3a1f4c9 (which is schema-only for the same reason).

Transaction handling: the `transactional_ddl = False` module attribute below
matches this repo's existing convention for enum ADD VALUE migrations (see
d6b8e3a1f4c9, f3a6c9d1b7e2, and ~25 others) but does NOT actually do
anything - verified directly against the installed Alembic source
(alembic/runtime/environment.py, migration.py): `transactional_ddl` is only
ever read as an env.py-level `context.configure(...)` argument or a dialect
default, never as a per-migration-module attribute. It's kept here purely
for stylistic consistency with the rest of the codebase, not because it has
an effect. The actual fix is the explicit `op.execute("COMMIT")` below,
which closes this migration's transaction before the ALTER TYPE statements
run, so a later migration/seed step in the same `alembic upgrade` invocation
never tries to use one of these values before it's committed.

Alembic head note: this repo currently has 5 unmerged migration heads (see
docs/known_issues.md). This migration branches off `d2a7c9e4f1b6`, which
`alembic current` confirms is the actual revision the target RDS database is
stamped at - not an arbitrary/assumed head. Apply with an explicit revision
id (`alembic upgrade e686c750b7b4`), not bare `head`, since bare `head` is
ambiguous while the 5-head situation is unresolved.

Revision ID: e686c750b7b4
Revises: d2a7c9e4f1b6
Create Date: 2026-08-05
"""
from alembic import op

revision = "e686c750b7b4"
down_revision = "d2a7c9e4f1b6"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_SCHEDULED'")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_RESCHEDULED'")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_CANCELLED'")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'CANDIDATE_SELECTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
