"""
Deferred finding P-M3: email/phone lookup hashes are keyed (HMAC-SHA256), not unsalted MD5 - so a
stored hash can't be reversed by hashing likely emails or phone numbers without the key.
"""
import hashlib
import hmac

import pytest

from app.core import encryption_service as mod
from app.core.encryption_service import EncryptionService


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("LOOKUP_HASH_KEY", "k1")
    mod.lookup_hash_key.cache_clear()
    yield
    mod.lookup_hash_key.cache_clear()


def test_the_hash_is_an_hmac_of_the_normalized_value(key):
    expected = hmac.new(b"k1", b"jane@example.invalid", hashlib.sha256).hexdigest()
    assert EncryptionService.generate_hash("  Jane@Example.invalid ") == expected
    assert EncryptionService.generate_hash("jane@example.invalid") != hashlib.md5(b"jane@example.invalid").hexdigest()
    assert len(expected) == 64                      # fits candidates.email_hash String(64)


def test_a_different_key_gives_a_different_hash(monkeypatch, key):
    first = EncryptionService.generate_hash("jane@example.invalid")
    monkeypatch.setenv("LOOKUP_HASH_KEY", "k2")
    mod.lookup_hash_key.cache_clear()
    assert EncryptionService.generate_hash("jane@example.invalid") != first


def test_without_an_override_the_key_derives_from_the_pii_key(monkeypatch):
    monkeypatch.delenv("LOOKUP_HASH_KEY", raising=False)
    monkeypatch.setattr(mod, "dotenv_values", lambda path: {})
    monkeypatch.setattr(mod, "resolve_key_material", lambda alias: b"pii-material")
    mod.lookup_hash_key.cache_clear()
    try:
        assert mod.lookup_hash_key() == hmac.new(b"pii-material", b"airs-lookup-hash-v1", hashlib.sha256).digest()
    finally:
        mod.lookup_hash_key.cache_clear()


def test_the_rehash_script_dry_run_writes_nothing(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from scripts import rehash_candidate_lookups as script
    candidate = SimpleNamespace(id="c", email_encrypted=b"e", phone_encrypted=None, encryption_key_id="k",
                                email_hash="old", phone_hash=None)
    db = MagicMock()
    db.query.return_value.all.return_value = [candidate]
    service = MagicMock()
    service.decrypt.return_value = "jane@example.invalid"
    service.generate_hash.return_value = "new"
    monkeypatch.setattr(script, "SessionLocal", lambda: db)
    monkeypatch.setattr(script, "EncryptionService", lambda repo: service)
    monkeypatch.setattr(script, "EncryptionKeyRepository", lambda d: None)
    assert script.main([]) == 0
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert script.main(["--apply"]) == 0
    db.commit.assert_called_once()
