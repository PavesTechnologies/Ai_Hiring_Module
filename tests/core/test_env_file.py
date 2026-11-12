"""AIRS_ENV_FILE: a git worktree reads the main checkout's .env instead of a copy (app/core/env_file.py)."""
import pytest

from app.core import encryption, encryption_service
from app.core.config import Settings
from app.core.env_file import ENV_FILE_VARIABLE, EnvFileMissing, env_file_path


def test_dot_env_in_the_working_directory_by_default(monkeypatch):
    monkeypatch.delenv(ENV_FILE_VARIABLE, raising=False)
    assert env_file_path() == ".env"
    monkeypatch.setenv(ENV_FILE_VARIABLE, "  ")
    assert env_file_path() == ".env"


def test_the_named_file_is_used(monkeypatch, tmp_path):
    named = tmp_path / "main.env"
    named.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(named))
    assert env_file_path() == str(named)


def test_a_named_file_that_does_not_exist_is_refused_not_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(tmp_path / "missing.env"))
    with pytest.raises(EnvFileMissing, match=ENV_FILE_VARIABLE):
        env_file_path()


def test_settings_read_the_same_file():
    assert Settings.model_config["env_file"] == env_file_path()


def test_key_material_comes_from_the_named_file_when_the_environment_has_none(monkeypatch, tmp_path):
    named = tmp_path / "main.env"
    named.write_text("ENCRYPTION_KEY_ENV_FILE_TEST=from-the-named-file\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(named))
    monkeypatch.delenv("ENCRYPTION_KEY_ENV_FILE_TEST", raising=False)
    assert encryption.resolve_key_material("env-file-test") == b"from-the-named-file"
    # the real environment still comes first
    monkeypatch.setenv("ENCRYPTION_KEY_ENV_FILE_TEST", "from-the-environment")
    assert encryption.resolve_key_material("env-file-test") == b"from-the-environment"


def test_the_lookup_hash_key_comes_from_the_named_file(monkeypatch, tmp_path):
    named = tmp_path / "main.env"
    named.write_text("LOOKUP_HASH_KEY=named-file-lookup-key\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(named))
    monkeypatch.delenv("LOOKUP_HASH_KEY", raising=False)
    encryption_service.lookup_hash_key.cache_clear()
    try:
        assert encryption_service.lookup_hash_key() == b"named-file-lookup-key"
    finally:
        encryption_service.lookup_hash_key.cache_clear()
