"""
Catalogue similarity search is exact (real Postgres + pgvector needed - like
the other tests/db tests, run against a migrated test database).

skill_ontology.embedding has an ivfflat index, which is approximate: with the
default ivfflat.probes = 1 a "top 3" query searched one list and could return
fewer than 3 rows (seen in e2e: 2), or miss the true nearest skill. The
suggestion, normalisation and scoring queries now sort on an expression the
index can't serve, so every active skill is compared.

Everything runs in one transaction that is rolled back.
"""
import random
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.db.session import SessionLocal
from app.models.skills import SkillOntology
from app.repositories.skill_repository import SkillRepository

DIMENSIONS = 384
QUERIES = 25


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _vector(rng) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(DIMENSIONS)]


@pytest.fixture
def catalogue(db):
    """At least 3 active skills with embeddings (added in this transaction), plus whatever is there."""
    rng = random.Random(7)
    added = []
    for n in range(3):
        skill = SkillOntology(canonical_name=f"zz exact search test {n} {uuid4().hex[:8]}", source="test",
                              is_active=True, embedding=_vector(rng))
        db.add(skill)
        added.append(skill)
    db.flush()
    return added


def test_asking_for_3_suggestions_returns_3(db, catalogue):
    repo = SkillRepository(db)
    rng = random.Random(11)
    counts = [len(repo.find_top_similar_canonical_skills(_vector(rng), limit=3)) for _ in range(QUERIES)]
    assert counts == [3] * QUERIES


def test_the_queries_do_not_use_the_approximate_index(db, catalogue):
    """EXPLAIN the exact SQL the repository sends: no ivfflat index scan in the plan."""
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "<=>" in statement:
            captured.append((statement, parameters))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        repo = SkillRepository(db)
        query = [0.1] * DIMENSIONS
        repo.find_top_similar_canonical_skills(query, limit=3)
        repo.find_by_embedding(query)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(captured) == 2
    for statement, parameters in captured:
        plan = "\n".join(row[0] for row in db.connection().exec_driver_sql("EXPLAIN " + statement, parameters))
        assert "idx_skill_ontology_embedding" not in plan, plan
