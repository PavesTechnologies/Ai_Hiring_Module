"""
Focused coverage for CandidateErasureService.request_erasure - the
"requested" phase, distinct from the existing erase_candidate hard-delete
flow (which has no prior test coverage either and is out of scope here).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.services.compliance.candidate_erasure_service import CandidateErasureService


def _make_candidate():
    return SimpleNamespace(id=uuid4(), updated_at=None)


def _make_resume():
    return SimpleNamespace(id=uuid4(), updated_at=None)


def _make_service():
    candidate_repo = MagicMock()
    resume_repo = MagicMock()
    campaign_candidate_repo = MagicMock()
    candidate_rejection_repo = MagicMock()
    consent_repo = MagicMock()
    email_notification_repo = MagicMock()
    celery_task_log_repo = MagicMock()
    dead_letter_queue_repo = MagicMock()
    storage_service = MagicMock()
    audit_service = MagicMock()

    service = CandidateErasureService(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        consent_repo=consent_repo,
        email_notification_repo=email_notification_repo,
        celery_task_log_repo=celery_task_log_repo,
        dead_letter_queue_repo=dead_letter_queue_repo,
        storage_service=storage_service,
        audit_service=audit_service,
    )
    return service, candidate_repo, resume_repo, audit_service


def test_request_erasure_zeroes_embeddings_and_marks_ineligible():
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate = _make_candidate()
    candidate_repo.get_by_id.return_value = candidate
    resumes = [_make_resume(), _make_resume()]
    resume_repo.get_all_versions_by_candidate.return_value = resumes
    resume_repo.zero_out_embeddings_for_candidate.return_value = 2

    service.request_erasure(candidate.id, actor_id="hr_admin_1", actor_role="HR_ADMIN", reason="GDPR request")

    resume_repo.zero_out_embeddings_for_candidate.assert_called_once_with(candidate.id)
    candidate_repo.update_erasure_fields.assert_called_once()
    update_kwargs = candidate_repo.update_erasure_fields.call_args.kwargs
    assert update_kwargs["erasure_requested_at"] is not None
    candidate_repo.commit.assert_called_once()


def test_request_erasure_never_touches_jd_embeddings():
    """No jd_repository dependency at all on this service - structurally guarantees jd_embeddings is untouched."""
    service, *_ = _make_service()
    assert not hasattr(service, "jd_repo")
    assert not hasattr(service, "jd_repository")


def test_request_erasure_bumps_candidate_and_resume_updated_at():
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate = _make_candidate()
    candidate_repo.get_by_id.return_value = candidate
    resume_a, resume_b = _make_resume(), _make_resume()
    resume_repo.get_all_versions_by_candidate.return_value = [resume_a, resume_b]

    service.request_erasure(candidate.id, actor_id="hr_admin_1", actor_role="HR_ADMIN")

    assert candidate.updated_at is not None
    assert resume_a.updated_at is not None
    assert resume_b.updated_at is not None


def test_request_erasure_writes_audit_log_with_requested_phase():
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate = _make_candidate()
    candidate_repo.get_by_id.return_value = candidate
    resume_repo.get_all_versions_by_candidate.return_value = []
    resume_repo.zero_out_embeddings_for_candidate.return_value = 0

    service.request_erasure(candidate.id, actor_id="hr_admin_1", actor_role="HR_ADMIN", reason="candidate request")

    audit_service.log.assert_called_once()
    audit_kwargs = audit_service.log.call_args.kwargs
    assert audit_kwargs["action_type"] == ActionType.CANDIDATE_DATA_ERASED
    assert audit_kwargs["entity_type"] == EntityType.CANDIDATE
    assert audit_kwargs["entity_id"] == candidate.id
    assert audit_kwargs["details"]["phase"] == "requested"
    assert audit_kwargs["details"]["reason"] == "candidate request"


def test_request_erasure_raises_not_found_for_missing_candidate():
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate_repo.get_by_id.return_value = None

    with pytest.raises(NotFoundError):
        service.request_erasure(uuid4(), actor_id="hr_admin_1", actor_role="HR_ADMIN")

    resume_repo.zero_out_embeddings_for_candidate.assert_not_called()


def test_request_erasure_rolls_back_on_failure():
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate = _make_candidate()
    candidate_repo.get_by_id.return_value = candidate
    resume_repo.get_all_versions_by_candidate.return_value = []
    resume_repo.zero_out_embeddings_for_candidate.side_effect = Exception("db exploded")

    with pytest.raises(Exception, match="db exploded"):
        service.request_erasure(candidate.id, actor_id="hr_admin_1", actor_role="HR_ADMIN")

    candidate_repo.rollback.assert_called_once()
    candidate_repo.commit.assert_not_called()


def test_request_erasure_never_deletes_resume_embeddings_rows():
    """Retained for referential integrity - the erasure-requested phase must never call the hard-delete method."""
    service, candidate_repo, resume_repo, audit_service = _make_service()
    candidate = _make_candidate()
    candidate_repo.get_by_id.return_value = candidate
    resume_repo.get_all_versions_by_candidate.return_value = []

    service.request_erasure(candidate.id, actor_id="hr_admin_1", actor_role="HR_ADMIN")

    resume_repo.delete_embeddings_by_candidate.assert_not_called()
