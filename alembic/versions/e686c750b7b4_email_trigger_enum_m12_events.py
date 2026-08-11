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

Alembic head note: this repo previously had 5+ unmerged migration heads (see
docs/known_issues.md). This migration originally branched off `d2a7c9e4f1b6`,
verified live via `alembic current` at the time. A subsequent merge (PR #88)
squashed the migration history down to a single `7043b9ed5abe_initial_schema`
root and deleted `d2a7c9e4f1b6` along with everything else that predated it -
re-parented onto that new root 2026-08-07. Unlike the sibling `09f831e39061`
re-parent, this migration's 4 values are NOT present in the squashed initial
schema's email_trigger_event_enum (verified: it only has CANDIDATE_REJECTED/
UPLOAD_PERMANENTLY_FAILED), so this migration is still functionally required,
not a no-op.

Revision ID: e686c750b7b4
Revises: 7043b9ed5abe
Create Date: 2026-08-05
"""
from alembic import op

revision = "e686c750b7b4"
down_revision = "7043b9ed5abe"
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
