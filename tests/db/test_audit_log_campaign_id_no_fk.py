"""
audit_log.campaign_id is a bare id like entity_id (migration b7d9f1a3c5e8):
audit_log is append-only, so a foreign key from it would make any audited
campaign undeletable. Model metadata and the migration chain only - no DB.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.compliance import AuditLog

ROOT = Path(__file__).resolve().parents[2]


def test_the_model_has_no_foreign_key_on_campaign_id():
    assert not AuditLog.__table__.c.campaign_id.foreign_keys
    assert AuditLog.__table__.c.campaign_id.nullable


def test_the_migration_is_the_single_head_and_drops_the_constraint():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["b7d9f1a3c5e8"]
    revision = script.get_revision("b7d9f1a3c5e8")
    assert revision.down_revision == "d6f8a0c2e4b7"
    source = Path(revision.path).read_text(encoding="utf-8")
    assert 'op.drop_constraint("audit_log_campaign_id_fkey", "audit_log", type_="foreignkey")' in source
