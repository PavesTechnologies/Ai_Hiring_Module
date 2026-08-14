"""
Epic 3 Fix 4: audit_log immutability is enforced by 3 Postgres triggers
(migration 7b3f6a92e1c4), not application code - this specific class of
guarantee (can a real UPDATE/DELETE/TRUNCATE statement actually be
rejected by the database itself) cannot be verified through a mock, so
this is the first real-DB-session test in this codebase (every other
existing test is MagicMock-based). Every test here runs its mutation
attempt inside a SAVEPOINT (db.begin_nested()) nested inside a transaction
that is always rolled back in a fixture teardown - no row is ever left
behind and no real row is ever actually touched, regardless of outcome.
"""
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import SessionLocal


@pytest.fixture
def db():
    """
    Real session wrapped in one outer transaction, rolled back at
    teardown - the entire test body's effects (including the row inserted
    in test_insert_still_succeeds) are undone unconditionally.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _insert_test_row(db, row_id):
    db.execute(
        text("""
            INSERT INTO audit_log (id, actor_id, actor_role, action_type, entity_type, entity_id)
            VALUES (:id, NULL, 'SYSTEM', 'CANDIDATE_ADDED', 'CANDIDATE', :id)
        """),
        {"id": str(row_id)},
    )


def test_insert_still_succeeds(db):
    """The only legitimate operation - must be completely unaffected."""
    row_id = uuid4()
    _insert_test_row(db, row_id)

    count = db.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id = :id"), {"id": str(row_id)},
    ).scalar()
    assert count == 1


def test_update_is_rejected(db):
    row_id = uuid4()
    _insert_test_row(db, row_id)

    with pytest.raises(DBAPIError, match="audit_log rows are immutable - UPDATE is not permitted"):
        with db.begin_nested():
            db.execute(
                text("UPDATE audit_log SET actor_role = 'HACKED' WHERE id = :id"), {"id": str(row_id)},
            )


def test_delete_is_rejected(db):
    row_id = uuid4()
    _insert_test_row(db, row_id)

    with pytest.raises(DBAPIError, match="audit_log rows are immutable - DELETE is not permitted"):
        with db.begin_nested():
            db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": str(row_id)})


def test_truncate_is_rejected(db):
    with pytest.raises(DBAPIError, match="audit_log rows are immutable - TRUNCATE is not permitted"):
        with db.begin_nested():
            db.execute(text("TRUNCATE audit_log"))


def test_alter_table_ddl_is_unaffected_by_the_dml_triggers(db):
    """
    Triggers fire on DML (INSERT/UPDATE/DELETE/TRUNCATE), never on DDL -
    a future migration adding/dropping a column must not be blocked by
    this. Verified directly rather than just asserted, given how much
    migration trouble this session has already had.
    """
    with db.begin_nested():
        db.execute(text("ALTER TABLE audit_log ADD COLUMN _immutability_test_probe integer"))
        exists = db.execute(
            text("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'audit_log' AND column_name = '_immutability_test_probe'
            """),
        ).fetchone()
        assert exists is not None
        db.execute(text("ALTER TABLE audit_log DROP COLUMN _immutability_test_probe"))
