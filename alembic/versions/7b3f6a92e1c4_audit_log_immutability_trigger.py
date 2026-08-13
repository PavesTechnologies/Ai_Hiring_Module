"""audit_log immutability trigger

Epic 3 Fix 4: "immutable" was pure convention - nothing in the DB or the
ORM stopped an UPDATE/DELETE/TRUNCATE on audit_log, only the fact that no
current code path happens to issue one (re-confirmed fresh: AuditRepository
still only ever create()/SELECT/save() - no update/delete anywhere, even
after Fix 1/2 touched several audit call sites).

Decision: trigger, not REVOKE. Investigated first, per the actual
deployment shape rather than guessing:

  - app/db/database.py's engine and alembic/env.py's migration connection
    both build their URL from the exact same DB_USER/DB_PASSWORD/DB_HOST/
    DB_PORT/DB_NAME/DB_SSLMODE keys in the same .env file - runtime and
    migrations share one DB role, not two. Confirmed live: SELECT
    current_user / session_user both return 'admin_paves'.
  - That role isn't even a narrowly-scoped app role - `pg_has_role`
    confirms admin_paves is a member of rds_superuser, pg_database_owner,
    pg_write_all_data, etc. It's this RDS instance's admin account.
  - Revoking UPDATE/DELETE/TRUNCATE from that role would (a) break every
    future migration that legitimately needs to ALTER this table, since
    it's the exact same role Alembic runs as, and (b) likely not even hold
    reliably given the role's own admin-tier membership. A trigger fires
    regardless of the calling role's grants and can be dropped explicitly
    and deliberately by a migration when a genuine schema change is
    needed - the correct mechanism for a single-shared-role deployment.

Historical mis-attributed rows from Fix 2 (CAMPAIGN_AUTO_CLOSED,
STALLED_CANDIDATES_ALERT, CAMPAIGN_HEALTH_ALERT,
CAMPAIGN_RESUBMISSION_DETECTED with actor_id wrongly set to a campaign's
created_by): re-checked immediately before writing this migration - zero
rows exist for any of those 4 action types on this database. There is
nothing to backfill here. This does NOT rule out other environments where
those scheduled jobs may have already run pre-fix; if such rows are ever
found, correcting them requires an UPDATE issued *before* this migration
enforces immutability (or via an explicit drop-trigger/fix/recreate-trigger
migration afterward) - flagged, not silently decided, since backfilling
history is a separate call from enforcing immutability going forward.

Three triggers share one function: UPDATE and DELETE are row-level
(FOR EACH ROW); TRUNCATE must be statement-level in Postgres (FOR EACH
STATEMENT) - a row-level trigger cannot be defined on TRUNCATE at all.
TG_OP in the raised exception reports which operation was attempted.

Triggers fire on DML, not DDL - a future ALTER TABLE (e.g. adding a
column) is completely unaffected by any of this; only UPDATE/DELETE/
TRUNCATE statements against audit_log rows are blocked. Verified in this
migration's own test suite, not just asserted here.

This is the first trigger anywhere in this codebase's schema (confirmed:
0 non-internal triggers existed anywhere in the database before this).

Revision ID: 7b3f6a92e1c4
Revises: c8e1a4f97d52
Create Date: 2026-08-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b3f6a92e1c4'
down_revision: Union[str, Sequence[str], None] = 'c8e1a4f97d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_log_prevent_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log rows are immutable - % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_update
        BEFORE UPDATE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_prevent_mutation();
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_delete
        BEFORE DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_prevent_mutation();
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_prevent_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_prevent_mutation()")
