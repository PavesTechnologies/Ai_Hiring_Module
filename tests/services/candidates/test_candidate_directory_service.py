from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.candidates import ParseStatus
from app.services.candidates.candidate_directory_service import CandidateDirectoryService

"""
GET /candidates (Global Candidates directory) - independent of any campaign
or Talent Pool concept. Never selects a resume for a campaign, never
computes a campaign score - both explicitly verified below by asserting the
relevant repositories/services are never touched.
"""


def _make_candidate(**overrides):
    defaults = dict(
        id=uuid4(),
        full_name_encrypted=b"encrypted-name",
        email_encrypted=b"encrypted-email",
        encryption_key_id=uuid4(),
        jurisdiction="GLOBAL",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_resume(candidate_id, **overrides):
    defaults = dict(
        id=uuid4(),
        candidate_id=candidate_id,
        version_number=2,
        parse_status=ParseStatus.PARSED,
        parsed_json={
            "location": "Bengaluru",
            "total_experience_years": 5.0,
            "work_experience": [{"title": "Backend Engineer", "is_current": True}],
        },
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_service(candidate_repo=None, resume_repo=None, encryption_service=None):
    if encryption_service is None:
        encryption_service = MagicMock()
        encryption_service.decrypt.side_effect = (
            lambda ciphertext, key_id: "Jane Doe" if ciphertext == b"encrypted-name" else "jane@example.com"
        )
    resume_repo = resume_repo or MagicMock()
    if not isinstance(resume_repo.get_active_by_candidate_ids.return_value, dict):
        resume_repo.get_active_by_candidate_ids.return_value = {}
    if not isinstance(resume_repo.get_canonical_skills_by_resume_ids.return_value, dict):
        resume_repo.get_canonical_skills_by_resume_ids.return_value = {}

    return CandidateDirectoryService(
        candidate_repo=candidate_repo or MagicMock(),
        resume_repo=resume_repo,
        encryption_service=encryption_service,
    )


def test_list_candidates_returns_candidates_with_no_resume():
    candidate = _make_candidate()
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = [candidate]
    candidate_repo.count_search.return_value = 1
    service = make_service(candidate_repo=candidate_repo)

    result = service.list_candidates()

    assert result.total == 1
    item = result.items[0]
    assert item.candidate_id == candidate.id
    assert item.full_name == "Jane Doe"
    assert item.email == "j***@example.com"
    assert item.resume is None
    assert item.designation is None
    assert item.skills == []


def test_list_candidates_returns_candidates_with_an_active_resume():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = [candidate]
    candidate_repo.count_search.return_value = 1
    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate_ids.return_value = {candidate.id: resume}
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {resume.id: ["Python", "SQL"]}
    service = make_service(candidate_repo=candidate_repo, resume_repo=resume_repo)

    result = service.list_candidates()

    item = result.items[0]
    assert item.designation == "Backend Engineer"
    assert item.location == "Bengaluru"
    assert item.experience == 5.0
    assert item.resume.resume_id == resume.id
    assert item.resume.version_number == 2
    assert item.resume.parse_status == ParseStatus.PARSED
    assert item.skills == ["Python", "SQL"]


def test_list_candidates_does_not_require_or_use_campaign_id():
    """No campaign_id param exists on list_candidates at all - a signature-level guarantee."""
    import inspect
    signature = inspect.signature(CandidateDirectoryService.list_candidates)
    assert "campaign_id" not in signature.parameters


def test_list_candidates_never_touches_campaign_membership():
    candidate = _make_candidate()
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = [candidate]
    candidate_repo.count_search.return_value = 1
    service = make_service(candidate_repo=candidate_repo)

    service.list_candidates()

    # No campaign_candidate_repo/resume_selection_service dependency exists
    # on this service at all - nothing to assert "not called" on beyond
    # confirming the constructor never wired one in.
    assert not hasattr(service, "campaign_candidate_repo")
    assert not hasattr(service, "resume_selection_service")


def test_list_candidates_batches_resume_and_skills_lookups_once_per_page():
    candidates = [_make_candidate() for _ in range(3)]
    resumes = {c.id: _make_resume(c.id) for c in candidates}
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = candidates
    candidate_repo.count_search.return_value = 3
    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate_ids.return_value = resumes
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    service = make_service(candidate_repo=candidate_repo, resume_repo=resume_repo)

    service.list_candidates(page=1, size=20)

    resume_repo.get_active_by_candidate_ids.assert_called_once()
    resume_repo.get_canonical_skills_by_resume_ids.assert_called_once()
    assert len(resume_repo.get_active_by_candidate_ids.call_args.args[0]) == 3


def test_list_candidates_passes_through_email_hash_and_jurisdiction_filters():
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = []
    candidate_repo.count_search.return_value = 0
    service = make_service(candidate_repo=candidate_repo)

    service.list_candidates(email_hash="abc123", jurisdiction="EU", page=2, size=10)

    candidate_repo.search.assert_called_once_with(email_hash="abc123", jurisdiction="EU", name=None, page=2, size=10)
    candidate_repo.count_search.assert_called_once_with(email_hash="abc123", jurisdiction="EU", name=None)


def test_list_candidates_pagination_fields_reflected_in_response():
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = []
    candidate_repo.count_search.return_value = 57
    service = make_service(candidate_repo=candidate_repo)

    result = service.list_candidates(page=3, size=10)

    assert result.page == 3
    assert result.size == 10
    assert result.total == 57


def test_list_candidates_falls_back_to_placeholder_when_name_undecryptable():
    from app.core.encryption_service import DecryptionError

    candidate = _make_candidate()
    candidate_repo = MagicMock()
    candidate_repo.search.return_value = [candidate]
    candidate_repo.count_search.return_value = 1
    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = DecryptionError("bad key")
    service = make_service(candidate_repo=candidate_repo, encryption_service=encryption_service)

    result = service.list_candidates()

    assert result.items[0].full_name == "[undecryptable]"
    assert result.items[0].email is None
