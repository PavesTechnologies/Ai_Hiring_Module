"""
Every parse records the hash of the resume's redacted text on the resume
(resumes.parse_text_hashes) right after redaction - before the AI extraction
can store a parse-cache entry under it - so erasure can always find that
entry. Model, migration, repository query (compiled, never run), the
pipeline hook and erasure's lookup. No database, no email.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from app.models.candidates import Resume
from app.services.skills.skill_evidence_service import text_hash

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


def test_the_column_is_a_nullable_array_of_hashes():
    column = Resume.__table__.c.parse_text_hashes
    assert column.nullable and type(column.type).__name__ == "ARRAY"


def test_the_migration_adds_and_backfills_the_column():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    # On the single line of history (later migrations build on it).
    assert len(script.get_heads()) == 1
    assert "c9e1a3b5d7f2" in {r.revision for r in script.walk_revisions()}
    revision = script.get_revision("c9e1a3b5d7f2")
    assert revision.down_revision == "b7d9f1a3c5e8"
    source = Path(revision.path).read_text(encoding="utf-8")
    assert 'op.add_column("resumes", sa.Column("parse_text_hashes"' in source
    for table in ("resume_skill_evidence", "resume_provisional_skill_scans", "core_skill_confirmations"):
        assert f"FROM {table}" in source


def test_recording_a_hash_appends_it_once():
    from app.repositories.resume_repository import ResumeRepository
    db = MagicMock()
    ResumeRepository(db).record_parse_text_hash(uuid4(), "a" * 64)
    stmt = db.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
    assert sql.startswith("update resumes set parse_text_hashes=array_append(")
    assert "coalesce(resumes.parse_text_hashes" in sql
    assert "resumes.parse_text_hashes is null or not (" in sql and "= any (resumes.parse_text_hashes))" in sql
    db.flush.assert_called_once()


def _pipeline():
    from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.pii_redaction_service = MagicMock(redact=MagicMock(return_value="redacted resume text"))
    pipeline.resume_repository = MagicMock()
    return pipeline


def test_redaction_records_the_hash_and_commits_it():
    pipeline = _pipeline()
    resume_id = uuid4()
    context = SimpleNamespace(resume_id=resume_id, cleaned_text="clean", pii_findings=[], redacted_text=None)

    pipeline._run_pii_redaction(context)

    assert context.redacted_text == "redacted resume text"
    pipeline.resume_repository.record_parse_text_hash.assert_called_once_with(resume_id, text_hash("redacted resume text"))
    pipeline.resume_repository.commit.assert_called_once()


def test_the_hash_is_the_parse_cache_key_hash():
    # the cache stores text_hash(context.redacted_text) - the same function, the same text
    from app.services.resume import resume_processing_pipeline as module
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "text_hash=text_hash(context.redacted_text)" in source
    assert "record_parse_text_hash(context.resume_id, text_hash(context.redacted_text))" in source


def test_a_failure_to_record_never_blocks_the_parse():
    pipeline = _pipeline()
    pipeline.resume_repository.record_parse_text_hash.side_effect = RuntimeError("db hiccup")
    context = SimpleNamespace(resume_id=uuid4(), cleaned_text="clean", pii_findings=[], redacted_text=None)
    pipeline._run_pii_redaction(context)                                     # no exception
    assert context.redacted_text == "redacted resume text"
    pipeline.resume_repository.rollback.assert_called_once()
