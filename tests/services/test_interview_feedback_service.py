"""
M12 Step 3 - InterviewFeedbackService. Uses the real sign_feedback_token/
verify_feedback_token round-trip (not mocked out) for the valid-token
tests, matching this session's preference for exercising real crypto/
schema logic rather than mocking domain behavior away - only the
repositories/collaborators are MagicMocks, per this project's universal
convention.
"""
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core import feedback_token
from app.core.config import settings
from app.exceptions.campaign_exceptions import CampaignException
from app.models.interview import InterviewFeedbackRecommendation
from app.schemas.interview_feedback_schema import SubmitFeedbackRequest
from app.services.interview_feedback_service import InterviewFeedbackService


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(settings, "feedback_token_signing_key", "test-feedback-signing-key")


def _make_schedule(**overrides):
    # end_at defaults to safely in the past - submit_feedback's "hasn't
    # ended yet" guard would otherwise reject every submit_feedback test
    # that doesn't care about timing. Tests that specifically exercise the
    # None/future-end_at cases override it explicitly.
    defaults = dict(
        id=uuid4(), campaign_candidate_id=uuid4(), round_number=1,
        interview_type="Technical Interview",
        start_at=None, end_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        timezone="UTC",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_interviewer(interview_id, **overrides):
    defaults = dict(id=uuid4(), interview_id=interview_id, name="Priya Sharma", email="priya@example.com")
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_env(schedule=None, interviewer=None, feedback_rows=None):
    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_by_id.return_value = schedule
    interview_schedule_repo.get_interviewer_by_id.return_value = interviewer
    interview_schedule_repo.get_interviewers.return_value = [interviewer] if interviewer else []

    campaign_candidate = SimpleNamespace(
        id=schedule.campaign_candidate_id if schedule else uuid4(),
        campaign_id=uuid4(), candidate_id=uuid4(),
    )
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    campaign = SimpleNamespace(id=campaign_candidate.campaign_id, hiring_manager_id="hm-1")
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign

    candidate = SimpleNamespace(full_name_encrypted=b"enc(Jordan Lee)", encryption_key_id=uuid4())
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    encryption_service = MagicMock()
    encryption_service.decrypt.return_value = "Jordan Lee"

    interview_feedback_repo = MagicMock()
    interview_feedback_repo.get_by_interview_schedule_id.return_value = feedback_rows or []
    interview_feedback_repo.get_by_interview_schedule_id_and_interviewer_id.return_value = None

    audit_service = MagicMock()

    service = InterviewFeedbackService(
        interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo,
        campaign_repo, candidate_repo, encryption_service, audit_service,
    )
    return service, interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo, audit_service, campaign_candidate


# ----------------------------------------------------------------------
# get_feedback_form_context - valid token round-trip.
# ----------------------------------------------------------------------

def test_get_feedback_form_context_returns_candidate_name_and_round_info():
    schedule = _make_schedule(interview_type="Panel Round", start_at=None, end_at=None)
    interviewer = _make_interviewer(schedule.id, name="Priya Sharma")
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    result = service.get_feedback_form_context(token)

    assert result.candidate_name == "Jordan Lee"
    assert result.interview_type == "Panel Round"
    assert result.round_number == 1
    assert result.interviewer_name == "Priya Sharma"


def test_get_feedback_form_context_converts_the_stored_utc_instant_to_the_rounds_timezone():
    """
    Timezone-discrepancy fix: start_at/end_at are stored UTC - the
    interviewer must see the round's actual local time, not raw UTC.
    """
    schedule = _make_schedule(
        start_at=datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 25, 10, 30, tzinfo=timezone.utc),
        timezone="Asia/Kolkata",
    )
    interviewer = _make_interviewer(schedule.id)
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    result = service.get_feedback_form_context(token)

    assert result.date == datetime(2026, 8, 25).date()
    assert result.start_time == datetime(2026, 8, 25, 15, 0).time()
    assert result.end_time == datetime(2026, 8, 25, 16, 0).time()


def test_get_feedback_form_context_never_exposes_more_than_the_candidate_name():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    result = service.get_feedback_form_context(token)

    assert not hasattr(result, "candidate_email")
    assert not hasattr(result, "interviewers")
    assert not hasattr(result, "meeting_link")


def test_get_feedback_form_context_default_is_not_already_submitted():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    result = service.get_feedback_form_context(token)

    assert result.already_submitted is False
    assert result.existing_recommendation is None
    assert result.existing_submitted_at is None


# ----------------------------------------------------------------------
# Fix: GET must reflect "already submitted" - opening the same link twice
# previously showed a fillable form both times, only failing (409) at the
# last step, on submit.
# ----------------------------------------------------------------------

def test_get_feedback_form_context_reports_already_submitted():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    submitted_at = datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)
    existing = SimpleNamespace(
        recommendation=InterviewFeedbackRecommendation.SELECT, submitted_at=submitted_at,
    )
    interview_feedback_repo.get_by_interview_schedule_id_and_interviewer_id.return_value = existing

    result = service.get_feedback_form_context(token)

    assert result.already_submitted is True
    assert result.existing_recommendation == InterviewFeedbackRecommendation.SELECT
    assert result.existing_submitted_at == submitted_at
    interview_feedback_repo.get_by_interview_schedule_id_and_interviewer_id.assert_called_once_with(
        schedule.id, interviewer.id,
    )


def test_get_feedback_form_context_still_returns_normal_form_context_when_already_submitted():
    """Sent alongside the normal context, not instead of it - the frontend decides whether to show read-only round details on the "already submitted" screen."""
    schedule = _make_schedule(interview_type="Panel Round")
    interviewer = _make_interviewer(schedule.id, name="Priya Sharma")
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)
    interview_feedback_repo.get_by_interview_schedule_id_and_interviewer_id.return_value = SimpleNamespace(
        recommendation=InterviewFeedbackRecommendation.REJECT, submitted_at=None,
    )

    result = service.get_feedback_form_context(token)

    assert result.already_submitted is True
    assert result.candidate_name == "Jordan Lee"
    assert result.interview_type == "Panel Round"
    assert result.interviewer_name == "Priya Sharma"


# ----------------------------------------------------------------------
# Invalid/expired/tampered token - clean 404, not a raw crash.
# ----------------------------------------------------------------------

def test_get_feedback_form_context_raises_404_on_malformed_token():
    service, *_rest = _make_env()

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_form_context("not-a-valid-token")

    assert exc_info.value.status_code == 404


def test_get_feedback_form_context_raises_404_on_expired_token(monkeypatch):
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 2_000_000)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)
    monkeypatch.setattr(time, "time", real_time)

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_form_context(token)

    assert exc_info.value.status_code == 404


def test_get_feedback_form_context_raises_404_on_tampered_token():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)
    payload_b64, signature = token.split(".", 1)
    tampered = f"{payload_b64}x.{signature}"

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_form_context(tampered)

    assert exc_info.value.status_code == 404


def test_get_feedback_form_context_raises_404_when_interviewer_does_not_belong_to_that_round():
    """A validly-signed token whose interviewer belongs to a DIFFERENT round than it claims - not just any invalid signature."""
    schedule = _make_schedule()
    other_round_id = uuid4()
    mismatched_interviewer = _make_interviewer(other_round_id)  # interview_id != schedule.id
    service, *_rest = _make_env(schedule=schedule, interviewer=mismatched_interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, mismatched_interviewer.id)

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_form_context(token)

    assert exc_info.value.status_code == 404


def test_get_feedback_form_context_raises_404_when_schedule_no_longer_exists():
    service, interview_feedback_repo, interview_schedule_repo, *_rest = _make_env(schedule=None, interviewer=None)
    token = feedback_token.sign_feedback_token(uuid4(), uuid4())

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_form_context(token)

    assert exc_info.value.status_code == 404


# ----------------------------------------------------------------------
# submit_feedback
# ----------------------------------------------------------------------

def test_submit_feedback_creates_the_row_and_logs_audit_with_external_interviewer_actor():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule, interviewer=interviewer,
    )
    interview_feedback_repo.create.return_value = (SimpleNamespace(id=uuid4()), True)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.ADVANCE, notes="Strong"))

    interview_feedback_repo.create.assert_called_once_with(
        schedule.id, interviewer.id, InterviewFeedbackRecommendation.ADVANCE, "Strong",
    )
    audit_service.log.assert_called_once()
    call_kwargs = audit_service.log.call_args.kwargs
    assert call_kwargs["actor_id"] is None
    assert call_kwargs["actor_role"] == "EXTERNAL_INTERVIEWER"
    assert call_kwargs["entity_id"] == campaign_candidate.id
    interview_feedback_repo.commit.assert_called_once()


def test_submit_feedback_does_not_touch_interview_status_or_pipeline_stage():
    """Purely advisory - confirmed by asserting the repo's update-style methods are never called."""
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo, audit_service, _ = _make_env(
        schedule=schedule, interviewer=interviewer,
    )
    interview_feedback_repo.create.return_value = (SimpleNamespace(id=uuid4()), True)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.SELECT))

    interview_schedule_repo.update.assert_not_called()
    campaign_candidate_repo.update_pipeline_stage.assert_not_called()


def test_submit_feedback_raises_409_on_duplicate_submission_not_a_raw_integrity_error():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    interview_feedback_repo.create.return_value = (SimpleNamespace(id=uuid4()), False)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    with pytest.raises(CampaignException) as exc_info:
        service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.HOLD))

    assert exc_info.value.status_code == 409
    assert "already been submitted" in str(exc_info.value)
    interview_feedback_repo.rollback.assert_called_once()


# ----------------------------------------------------------------------
# Fix: submit_feedback must reject a submission before the interview has
# actually ended. Previously safe by construction (the link only ever
# reached an interviewer via a post-end_at reminder) - no longer
# guaranteed once the link can reach them earlier (e.g. via the calendar
# invite).
# ----------------------------------------------------------------------

def test_submit_feedback_rejects_before_the_interview_has_ended():
    schedule = _make_schedule(end_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    with pytest.raises(CampaignException) as exc_info:
        service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.ADVANCE))

    assert exc_info.value.status_code == 400
    assert "hasn't ended" in str(exc_info.value) or "has ended" in str(exc_info.value)
    interview_feedback_repo.create.assert_not_called()


def test_submit_feedback_rejects_when_end_at_is_unset():
    schedule = _make_schedule(end_at=None)
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    with pytest.raises(CampaignException) as exc_info:
        service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.ADVANCE))

    assert exc_info.value.status_code == 400
    interview_feedback_repo.create.assert_not_called()


def test_submit_feedback_succeeds_once_the_interview_has_ended():
    """No regression on the existing valid-submission path - end_at safely in the past."""
    schedule = _make_schedule(end_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    interviewer = _make_interviewer(schedule.id)
    service, interview_feedback_repo, *_rest = _make_env(schedule=schedule, interviewer=interviewer)
    interview_feedback_repo.create.return_value = (SimpleNamespace(id=uuid4()), True)
    token = feedback_token.sign_feedback_token(schedule.id, interviewer.id)

    service.submit_feedback(token, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.ADVANCE))

    interview_feedback_repo.create.assert_called_once()


def test_submit_feedback_raises_404_on_invalid_token():
    service, *_rest = _make_env()

    with pytest.raises(CampaignException) as exc_info:
        service.submit_feedback("garbage-token", SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.REJECT))

    assert exc_info.value.status_code == 404


def test_two_interviewers_on_the_same_round_can_each_submit_independently():
    schedule = _make_schedule()
    interviewer_a = _make_interviewer(schedule.id, name="Alice")
    interviewer_b = _make_interviewer(schedule.id, name="Bob")

    service_a, repo_a, *_ = _make_env(schedule=schedule, interviewer=interviewer_a)
    repo_a.create.return_value = (SimpleNamespace(id=uuid4()), True)
    token_a = feedback_token.sign_feedback_token(schedule.id, interviewer_a.id)
    service_a.submit_feedback(token_a, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.ADVANCE))
    repo_a.create.assert_called_once_with(schedule.id, interviewer_a.id, InterviewFeedbackRecommendation.ADVANCE, None)

    service_b, repo_b, *_ = _make_env(schedule=schedule, interviewer=interviewer_b)
    repo_b.create.return_value = (SimpleNamespace(id=uuid4()), True)
    token_b = feedback_token.sign_feedback_token(schedule.id, interviewer_b.id)
    service_b.submit_feedback(token_b, SubmitFeedbackRequest(recommendation=InterviewFeedbackRecommendation.REJECT))
    repo_b.create.assert_called_once_with(schedule.id, interviewer_b.id, InterviewFeedbackRecommendation.REJECT, None)


# ----------------------------------------------------------------------
# get_feedback_for_round - authenticated HR/HM viewing.
# ----------------------------------------------------------------------

def test_get_feedback_for_round_returns_all_submitted_feedback():
    schedule = _make_schedule()
    interviewer = _make_interviewer(schedule.id, name="Priya", email="priya@example.com")
    feedback_row = SimpleNamespace(
        id=uuid4(), interviewer_id=interviewer.id,
        recommendation=InterviewFeedbackRecommendation.ADVANCE, notes="Great", submitted_at="2026-08-18T10:00:00Z",
    )
    service, *_rest, campaign_candidate = _make_env(schedule=schedule, interviewer=interviewer, feedback_rows=[feedback_row])

    result = service.get_feedback_for_round(
        campaign_candidate.id, schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
    )

    assert len(result) == 1
    assert result[0].interviewer_name == "Priya"
    assert result[0].interviewer_email == "priya@example.com"
    assert result[0].recommendation == "ADVANCE"
    assert result[0].notes == "Great"


def test_get_feedback_for_round_returns_every_interviewers_feedback_not_just_one():
    """
    Direct answer to "does 2 interviewers both submitting show up as 2
    entries, not 1" - _make_env only wires a single interviewer, so this
    builds its own 2-interviewer/2-feedback-row environment rather than
    stretching that helper to fit.
    """
    schedule = _make_schedule()
    interviewer_a = _make_interviewer(schedule.id, name="Alice", email="alice@example.com")
    interviewer_b = _make_interviewer(schedule.id, name="Bob", email="bob@example.com")
    feedback_a = SimpleNamespace(
        id=uuid4(), interviewer_id=interviewer_a.id,
        recommendation=InterviewFeedbackRecommendation.ADVANCE, notes="Strong", submitted_at="2026-08-18T10:00:00Z",
    )
    feedback_b = SimpleNamespace(
        id=uuid4(), interviewer_id=interviewer_b.id,
        recommendation=InterviewFeedbackRecommendation.HOLD, notes="Mixed signals", submitted_at="2026-08-18T11:00:00Z",
    )

    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_by_id.return_value = schedule
    interview_schedule_repo.get_interviewers.return_value = [interviewer_a, interviewer_b]

    campaign_candidate = SimpleNamespace(id=schedule.campaign_candidate_id, campaign_id=uuid4(), candidate_id=uuid4())
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    interview_feedback_repo = MagicMock()
    interview_feedback_repo.get_by_interview_schedule_id.return_value = [feedback_a, feedback_b]

    service = InterviewFeedbackService(
        interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo,
        MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )

    result = service.get_feedback_for_round(
        campaign_candidate.id, schedule.id, actor_id="hr-1", actor_roles=["HR_ADMIN"],
    )

    assert len(result) == 2
    by_name = {r.interviewer_name: r for r in result}
    assert by_name["Alice"].recommendation == "ADVANCE"
    assert by_name["Bob"].recommendation == "HOLD"


def test_get_feedback_for_round_rejects_hiring_manager_who_does_not_own_the_campaign():
    schedule = _make_schedule()
    service, interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule,
    )
    campaign_candidate_repo.get_by_id.return_value = SimpleNamespace(id=campaign_candidate.id, campaign_id=uuid4())

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_for_round(campaign_candidate.id, schedule.id, actor_id="not-the-hm", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403


def test_get_feedback_for_round_allows_hr_admin_regardless_of_ownership():
    schedule = _make_schedule()
    service, *_rest, campaign_candidate = _make_env(schedule=schedule)

    result = service.get_feedback_for_round(
        campaign_candidate.id, schedule.id, actor_id="hr-1", actor_roles=["HR_ADMIN"],
    )

    assert result == []


def test_get_feedback_for_round_raises_404_when_interview_belongs_to_a_different_candidate():
    schedule = _make_schedule()
    service, *_rest, campaign_candidate = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.get_feedback_for_round(uuid4(), schedule.id, actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert exc_info.value.status_code == 404
