"""Settings phase 4: the database decides the embedding model; EMBEDDING_MODEL (.env) only seeds a fresh database."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.repositories.embedding_model_version_repository import EmbeddingModelVersionRepository


def _repo(active):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = active
    return EmbeddingModelVersionRepository(db), db


def test_a_different_env_model_never_replaces_the_active_one(caplog):
    active = SimpleNamespace(model_name="all-MiniLM-L6-v2", is_active=True, deprecated_at=None)
    repo, db = _repo(active)
    assert repo.sync_active_from_settings("bge-small-en-v1.5") is active
    assert active.is_active is True and active.deprecated_at is None
    db.add.assert_not_called()
    db.commit.assert_not_called()
    assert "differs from the active embedding model" in caplog.text


def test_a_fresh_database_gets_the_env_model():
    repo, db = _repo(None)
    version = repo.sync_active_from_settings("all-MiniLM-L6-v2")
    assert version.model_name == "all-MiniLM-L6-v2" and version.is_active is True
    db.add.assert_called_once()
