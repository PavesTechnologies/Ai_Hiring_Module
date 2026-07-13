from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import BadRequestError
from app.models.jd.job_descriptions import JDSourceFormat, JDVerificationStatus
from app.schemas.jd.request import EducationCriteria, UpdateJDRequest
from app.services.jd.jd_service import JDReprocessRequired, JDService, UpdateJDResponse


def _service():
    repository = MagicMock()
    hash_service = MagicMock()
    hash_service.generate_hash.side_effect = lambda text: f"hash({text})"
    audit_service = MagicMock()
    storage_service = MagicMock()
    return JDService(repository, hash_service, audit_service, storage_service), repository


def _existing_jd(**overrides):
    jd = MagicMock()
    jd.id = uuid4()
    jd.is_active_version = True
    jd.lineage_root_id = None
    jd.version_number = 1
    jd.raw_text = "original raw text"
    jd.source_format = JDSourceFormat.TEXT
    jd.file_path = None
    for key, value in overrides.items():
        setattr(jd, key, value)
    return jd


def _simulate_flush(jd):
    """create_job_description() normally assigns id via flush; the model's
    default=uuid.uuid4 only fires on actual INSERT, not construction — a
    mocked repository needs to simulate that explicitly."""
    if jd.id is None:
        jd.id = uuid4()
    return jd


def test_metadata_only_update_stays_synchronous():
    service, repository = _service()
    existing = _existing_jd()
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False
    repository.create_job_description.side_effect = _simulate_flush

    request = UpdateJDRequest(title="New Title", raw_text=None, jurisdiction="US")
    result = service.update_jd(jd_id=existing.id, request=request, updated_by="hr-1")

    assert isinstance(result, UpdateJDResponse)
    repository.deactivate_version.assert_called_once_with(existing)
    repository.commit.assert_called_once()


def test_metadata_only_update_carries_forward_raw_text_and_file():
    """Regression guard: UpdateJDRequest.raw_text is optional, but
    JobDescription.raw_text is NOT NULL — a metadata-only update must fall
    back to the existing version's raw_text/file, not pass through None."""
    service, repository = _service()
    existing = _existing_jd(raw_text="the real content", file_path="org_x/jd/doc.pdf")
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False
    captured = {}
    def _capture(jd):
        captured.update(raw=jd.raw_text, fp=jd.file_path)
        return _simulate_flush(jd)
    repository.create_job_description.side_effect = _capture

    request = UpdateJDRequest(title="New Title", raw_text=None, jurisdiction="US")
    result = service.update_jd(jd_id=existing.id, request=request, updated_by="hr-1")

    assert isinstance(result, UpdateJDResponse)
    assert captured["raw"] == "the real content"
    assert captured["fp"] == "org_x/jd/doc.pdf"


def test_raw_text_change_triggers_reprocess():
    service, repository = _service()
    existing = _existing_jd(raw_text="old text")
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False
    repository.get_duplicate_excluding_lineage.return_value = None

    request = UpdateJDRequest(title="Title", raw_text="brand new text", jurisdiction="US")
    result = service.update_jd(jd_id=existing.id, request=request, updated_by="hr-1")

    assert isinstance(result, JDReprocessRequired)
    assert result.raw_text == "brand new text"
    assert result.file_path is None
    assert result.existing_jd_id == existing.id
    assert result.version_number == existing.version_number + 1
    repository.create_job_description.assert_not_called()
    repository.commit.assert_not_called()


def test_resubmitting_identical_raw_text_does_not_trigger_reprocess():
    service, repository = _service()
    existing = _existing_jd(raw_text="same text")
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False
    repository.get_duplicate_excluding_lineage.return_value = None
    repository.create_job_description.side_effect = _simulate_flush

    request = UpdateJDRequest(title="Title", raw_text="same text", jurisdiction="US")
    result = service.update_jd(jd_id=existing.id, request=request, updated_by="hr-1")

    assert isinstance(result, UpdateJDResponse)


def test_new_file_triggers_reprocess_even_without_raw_text():
    service, repository = _service()
    existing = _existing_jd(file_path="org_x/jd/old.pdf")
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False

    request = UpdateJDRequest(title="Title", raw_text=None, jurisdiction="US")
    result = service.update_jd(
        jd_id=existing.id, request=request, updated_by="hr-1", file_path="org_x/jd/new.pdf",
    )

    assert isinstance(result, JDReprocessRequired)
    assert result.file_path == "org_x/jd/new.pdf"
    assert result.old_file_path == "org_x/jd/old.pdf"
    assert result.raw_text is None


@pytest.mark.parametrize("field", ["employment_type_note"])
def test_metadata_only_fields_never_trigger_reprocess(field):
    """title/jurisdiction/min_experience_years/education_criteria alone —
    none of these are pipeline triggers per the finalized design."""
    service, repository = _service()
    existing = _existing_jd()
    repository.get_by_id.return_value = existing
    repository.has_active_campaign.return_value = False
    repository.create_job_description.side_effect = _simulate_flush

    request = UpdateJDRequest(
        title="Changed Title",
        raw_text=None,
        jurisdiction="EU",
        min_experience_years=5.0,
        education_criteria=EducationCriteria(degree="BSc", field="CS"),
    )
    result = service.update_jd(jd_id=existing.id, request=request, updated_by="hr-1")

    assert isinstance(result, UpdateJDResponse)


def test_persist_processed_jd_rolls_back_on_failure():
    service, repository = _service()
    repository.get_by_content_hash.return_value = None
    repository.create_job_description.side_effect = RuntimeError("db exploded")

    with pytest.raises(RuntimeError):
        service.persist_processed_jd(
            title="T", raw_text="text", jurisdiction="US",
            min_experience_years=None, education_criteria=None,
            source_format=JDSourceFormat.TEXT, file_path=None,
            created_by="hr-1", content_hash="hash",
            extraction=MagicMock(required_skills=[], preferred_skills=[], model_dump=lambda mode: {}),
            skill_repository=MagicMock(), skill_matches=[],
            embedding=[0.1], embedding_model_version_id=uuid4(), input_text_hash="ih",
        )

    repository.rollback.assert_called_once()
    repository.commit.assert_not_called()


def test_reprocess_persistence_uses_lineage_scoped_duplicate_check_not_global():
    service, repository = _service()
    repository.get_duplicate_excluding_lineage.return_value = None
    repository.create_job_description.side_effect = _simulate_flush
    repository.get_by_id.return_value = _existing_jd()

    service.persist_processed_jd(
        title="T", raw_text="text", jurisdiction="US",
        min_experience_years=None, education_criteria=None,
        source_format=JDSourceFormat.TEXT, file_path=None,
        created_by="hr-1", content_hash="hash",
        extraction=MagicMock(required_skills=[], preferred_skills=[], model_dump=lambda mode: {}),
        skill_repository=MagicMock(), skill_matches=[],
        embedding=[0.1], embedding_model_version_id=uuid4(), input_text_hash="ih",
        existing_jd_id=uuid4(), version_number=2, parent_jd_id=uuid4(), lineage_root_id=uuid4(),
    )

    repository.get_duplicate_excluding_lineage.assert_called_once()
    repository.get_by_content_hash.assert_not_called()
    repository.deactivate_version.assert_called_once()
