from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.jd.jd_embedding_service import (
    JDEmbeddingService,
    JD_EMBEDDING_MAX_CHARS_KEY,
    _DEFAULT_JD_EMBEDDING_MAX_CHARS,
)


def _make_job_description(title="Python Developer (Fresher)", raw_text="Build things."):
    return SimpleNamespace(id=uuid4(), title=title, raw_text=raw_text)


def _make_jd_skill_row(canonical_name, mandatory):
    jd_skill = SimpleNamespace(mandatory=mandatory)
    ontology = SimpleNamespace(canonical_name=canonical_name)
    return jd_skill, ontology


def _harness(
    job_description=None,
    jd_skill_rows=None,
    max_chars_config=None,
    existing_embedding=None,
    embedding_model_version=None,
    existing_embedding_for_jd=None,
):
    job_description = job_description or _make_job_description()
    jd_repo = MagicMock()
    jd_repo.get_by_id.return_value = job_description
    jd_repo.get_active_embedding_model_version.return_value = (
        embedding_model_version or SimpleNamespace(id=uuid4())
    )
    jd_repo.get_embedding_by_content_hash.return_value = existing_embedding
    jd_repo.get_embedding_by_jd_id.return_value = existing_embedding_for_jd
    jd_repo.create_jd_embedding_idempotent.return_value = (MagicMock(), True)
    jd_repo.replace_jd_embedding.return_value = MagicMock()

    skill_repo = MagicMock()
    skill_repo.get_jd_skills_by_jd_id.return_value = jd_skill_rows or []

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = (
        {} if max_chars_config is None else {JD_EMBEDDING_MAX_CHARS_KEY: max_chars_config}
    )

    embedding_service = MagicMock()
    embedding_service.generate_embedding.return_value = [0.1] * 384

    service = JDEmbeddingService(jd_repo, skill_repo, config_repo, embedding_service)
    return service, job_description, jd_repo, skill_repo, config_repo, embedding_service


def test_raises_when_job_description_not_found():
    service, _, jd_repo, _, _, _ = _harness()
    jd_repo.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.generate_and_store_embedding(uuid4())


def test_generates_new_embedding_when_no_dedup_match():
    service, jd, jd_repo, _, _, embedding_service = _harness(existing_embedding=None)

    result = service.generate_and_store_embedding(jd.id)

    embedding_service.generate_embedding.assert_called_once()
    create_kwargs = jd_repo.create_jd_embedding_idempotent.call_args.kwargs
    assert create_kwargs["jd_id"] == jd.id
    assert create_kwargs["embedding"] == [0.1] * 384
    assert result is not None


def test_reuses_existing_vector_on_dedup_hit():
    existing = SimpleNamespace(id=uuid4(), embedding=[0.9] * 384)
    service, jd, jd_repo, _, _, embedding_service = _harness(existing_embedding=existing)

    service.generate_and_store_embedding(jd.id)

    embedding_service.generate_embedding.assert_not_called()
    create_kwargs = jd_repo.create_jd_embedding_idempotent.call_args.kwargs
    assert create_kwargs["embedding"] == [0.9] * 384


def test_splits_mandatory_and_preferred_skill_names_correctly():
    rows = [
        _make_jd_skill_row("Python", mandatory=True),
        _make_jd_skill_row("SQL", mandatory=True),
        _make_jd_skill_row("FastAPI", mandatory=False),
    ]
    service, jd, jd_repo, skill_repo, _, embedding_service = _harness(jd_skill_rows=rows)

    service.generate_and_store_embedding(jd.id)

    skill_repo.get_jd_skills_by_jd_id.assert_called_once_with(jd.id)
    # The hash/text is internal, but we can verify the embedding call
    # actually received text containing both skill lists correctly split.
    called_text = embedding_service.generate_embedding.call_args.args[0]
    assert "Required skills: Python, SQL." in called_text
    assert "Preferred skills: FastAPI." in called_text


def test_uses_configured_max_chars_to_truncate_raw_text():
    long_raw_text = "y" * 5000
    jd = _make_job_description(raw_text=long_raw_text)
    service, jd, jd_repo, _, config_repo, embedding_service = _harness(
        job_description=jd, max_chars_config="50",
    )

    service.generate_and_store_embedding(jd.id)

    called_text = embedding_service.generate_embedding.call_args.args[0]
    assert called_text.endswith("y" * 50)
    assert not called_text.endswith("y" * 51)


def test_falls_back_to_default_max_chars_when_config_missing():
    service, jd, jd_repo, _, config_repo, _ = _harness(max_chars_config=None)

    max_chars = service._read_max_chars()

    assert max_chars == _DEFAULT_JD_EMBEDDING_MAX_CHARS


def test_falls_back_to_default_max_chars_when_config_invalid():
    service, jd, jd_repo, _, config_repo, _ = _harness(max_chars_config="not-a-number")

    max_chars = service._read_max_chars()

    assert max_chars == _DEFAULT_JD_EMBEDDING_MAX_CHARS


def test_does_not_raise_when_create_returns_existing_row():
    service, jd, jd_repo, _, _, _ = _harness()
    jd_repo.create_jd_embedding_idempotent.return_value = (MagicMock(), False)

    result = service.generate_and_store_embedding(jd.id)

    assert result is not None


# ----------------------------------------------------------------------
# force_regenerate - the "jd_skills changed on an already-active JD"
# trigger. Default (force_regenerate=False) is the "JD activated" trigger,
# already covered by the tests above (all call generate_and_store_embedding
# with no force_regenerate arg).
# ----------------------------------------------------------------------

def test_skips_entirely_when_not_forced_and_embedding_already_exists():
    """
    The plain activation trigger must be a cheap no-op once a JD already
    has an embedding - never rebuilds text/hash, never touches the
    embedding model, regardless of whether jd_skills changed.
    """
    existing_for_jd = SimpleNamespace(id=uuid4(), embedding=[0.5] * 384)
    service, jd, jd_repo, skill_repo, _, embedding_service = _harness(
        existing_embedding_for_jd=existing_for_jd,
    )

    result = service.generate_and_store_embedding(jd.id, force_regenerate=False)

    assert result is existing_for_jd
    skill_repo.get_jd_skills_by_jd_id.assert_not_called()
    embedding_service.generate_embedding.assert_not_called()
    jd_repo.create_jd_embedding_idempotent.assert_not_called()
    jd_repo.replace_jd_embedding.assert_not_called()


def test_force_regenerate_rebuilds_text_even_when_embedding_already_exists():
    existing_for_jd = SimpleNamespace(id=uuid4(), embedding=[0.5] * 384)
    service, jd, jd_repo, skill_repo, _, embedding_service = _harness(
        existing_embedding_for_jd=existing_for_jd,
    )

    service.generate_and_store_embedding(jd.id, force_regenerate=True)

    skill_repo.get_jd_skills_by_jd_id.assert_called_once_with(jd.id)
    embedding_service.generate_embedding.assert_called_once()


def test_force_regenerate_overwrites_existing_row_via_replace_not_create():
    existing_for_jd = SimpleNamespace(id=uuid4(), embedding=[0.5] * 384)
    service, jd, jd_repo, _, _, embedding_service = _harness(
        existing_embedding_for_jd=existing_for_jd,
    )

    result = service.generate_and_store_embedding(jd.id, force_regenerate=True)

    jd_repo.replace_jd_embedding.assert_called_once()
    replace_kwargs = jd_repo.replace_jd_embedding.call_args.kwargs
    assert replace_kwargs["jd_id"] == jd.id
    assert replace_kwargs["embedding"] == [0.1] * 384
    jd_repo.create_jd_embedding_idempotent.assert_not_called()
    assert result is jd_repo.replace_jd_embedding.return_value


def test_force_regenerate_still_dedups_by_content_hash_before_calling_model():
    """
    Even when forced, if the freshly-built text's hash matches some other
    existing jd_embeddings row, the embedding model itself is still never
    called - only the persistence step differs (replace vs create).
    """
    existing_for_jd = SimpleNamespace(id=uuid4(), embedding=[0.5] * 384)
    dedup_match = SimpleNamespace(id=uuid4(), embedding=[0.9] * 384)
    service, jd, jd_repo, _, _, embedding_service = _harness(
        existing_embedding_for_jd=existing_for_jd, existing_embedding=dedup_match,
    )

    service.generate_and_store_embedding(jd.id, force_regenerate=True)

    embedding_service.generate_embedding.assert_not_called()
    replace_kwargs = jd_repo.replace_jd_embedding.call_args.kwargs
    assert replace_kwargs["embedding"] == [0.9] * 384


def test_force_regenerate_calls_replace_even_when_jd_never_embedded_before():
    """
    A jd_id with no prior embedding at all still goes through
    replace_jd_embedding under force_regenerate - that repository method
    itself falls back to inserting when no row exists yet (see
    test_jd_repository_embedding_dedup.py), so the service never needs to
    choose between create/replace itself.
    """
    service, jd, jd_repo, _, _, _ = _harness(existing_embedding_for_jd=None)

    service.generate_and_store_embedding(jd.id, force_regenerate=True)

    jd_repo.replace_jd_embedding.assert_called_once()
    jd_repo.create_jd_embedding_idempotent.assert_not_called()
