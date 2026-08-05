"""
Focused coverage for the EMBED_JD auto-enqueue hook added to
JDService.update_jd's metadata-only path - the only path that creates a
brand-new JD version without ever giving it an embedding of its own
(unlike create/reprocess, which already gets one inline).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.jd.job_descriptions import JDSourceFormat, JDVerificationStatus
from app.schemas.jd.request import EducationCriteria, UpdateJDRequest
from app.services.embedding_queue_service import JDEmbeddingQueueError
from app.services.jd.jd_service import JDService


def _make_request():
    return UpdateJDRequest(
        title="Updated Backend Engineer",
        raw_text=None,
        jurisdiction="US",
        min_experience_years=1.0,
        max_experience_years=5.0,
        notice_period=30,
        education_criteria=EducationCriteria(degree="Bachelor's", field="CS"),
        prompt_template_id=uuid4(),
    )


def _make_existing_jd():
    return SimpleNamespace(
        id=uuid4(),
        lineage_root_id=None,
        version_number=1,
        is_active_version=True,
        source_format=JDSourceFormat.TEXT,
        file_path=None,
        original_filename=None,
        raw_text="Original JD text.",
        extracted_json={},
        required_skills={},
        is_verified=JDVerificationStatus.VERIFIED,
        jurisdiction="US",
        prompt_template_id=uuid4(),
    )


def _set_id_and_return(jd):
    # Mirrors what the real JDRepository.create_job_description does
    # (add/flush/refresh) - populates the DB-generated id, since this
    # mock never actually flushes to the database.
    jd.id = jd.id if getattr(jd, "id", None) is not None else uuid4()
    return jd


def _make_service(embedding_queue_service=None):
    repository = MagicMock()
    repository.has_active_campaign.return_value = False
    repository.create_job_description.side_effect = _set_id_and_return
    audit_service = MagicMock()
    prompt_template_repository = MagicMock()

    service = JDService(
        repository=repository,
        hash_service=MagicMock(),
        audit_service=audit_service,
        storage_service=MagicMock(),
        prompt_template_repository=prompt_template_repository,
        embedding_queue_service=embedding_queue_service,
    )
    return service, repository


def test_metadata_only_update_enqueues_embed_jd_after_commit():
    queue_service_mock = MagicMock()
    service, repository = _make_service(embedding_queue_service=queue_service_mock)
    existing_jd = _make_existing_jd()
    repository.get_by_id.return_value = existing_jd

    with patch(
        "app.services.jd.jd_service.validate_prompt_template_selection",
        return_value=SimpleNamespace(name="JD Parse v1"),
    ):
        response = service.update_jd(existing_jd.id, _make_request(), updated_by="hr_user")

    repository.commit.assert_called_once()
    queue_service_mock.queue_jd_embedding.assert_called_once_with(response.id, force_regenerate=False)


def test_embed_jd_enqueue_failure_never_crashes_or_masks_successful_update():
    queue_service_mock = MagicMock()
    queue_service_mock.queue_jd_embedding.side_effect = JDEmbeddingQueueError(
        "broker unreachable", jd_id=uuid4(), task_id=uuid4(),
    )
    service, repository = _make_service(embedding_queue_service=queue_service_mock)
    existing_jd = _make_existing_jd()
    repository.get_by_id.return_value = existing_jd

    with patch(
        "app.services.jd.jd_service.validate_prompt_template_selection",
        return_value=SimpleNamespace(name="JD Parse v1"),
    ):
        # Must not raise - the metadata update already committed successfully.
        response = service.update_jd(existing_jd.id, _make_request(), updated_by="hr_user")

    assert response is not None
    repository.commit.assert_called_once()


def test_defaults_to_a_real_embedding_queue_service_when_not_injected():
    """
    embedding_queue_service is optional so every existing JDService(...)
    call site keeps working unchanged - it must default to a real,
    functioning EmbeddingQueueService instance, not None.
    """
    from app.services.embedding_queue_service import EmbeddingQueueService

    service, _ = _make_service(embedding_queue_service=None)

    assert isinstance(service.embedding_queue_service, EmbeddingQueueService)
