from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.interview_feedback_request_sweep_service import InterviewFeedbackRequestSweepService

MODULE = "app.services.interview_feedback_request_sweep_service"

"""
Epic 5 Step 4 - InterviewFeedbackRequestSweepService.run(). CANCELLED-
round and future-end_at exclusion are real behaviors of
get_ended_active_rounds' own query (tested directly in
test_interview_schedule_repository.py). "Who still needs asking" (already
gave feedback, already been emailed) is now resolved by the shared
queue_pending_feedback_requests_for_round() (tested directly in
test_interview_feedback_request_emails.py, since it's also used by the
manual "Request Feedback" trigger) - this suite covers only what the
sweep itself decides: which ended rounds to hand off, and how to skip
ones with nothing to hand off (no interviewers, no campaign_candidate).
"""


def _schedule(**overrides):
    defaults = dict(id=uuid4(), campaign_candidate_id=uuid4())
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _interviewer(**overrides):
    defaults = dict(id=uuid4(), name="Interviewer", email="i@example.com")
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_service(ended_rounds, interviewers_by_schedule=None, campaign_candidates_by_id=None):
    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_ended_active_rounds.return_value = ended_rounds
    interview_schedule_repo.get_active_interviewers.side_effect = lambda schedule_id: (interviewers_by_schedule or {}).get(schedule_id, [])

    interview_feedback_repo = MagicMock()

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = lambda cc_id: (campaign_candidates_by_id or {}).get(
        cc_id, SimpleNamespace(id=cc_id, candidate_id=uuid4()),
    )

    service = InterviewFeedbackRequestSweepService(
        MagicMock(), interview_schedule_repo, interview_feedback_repo, campaign_candidate_repo,
    )
    return service, interview_schedule_repo, interview_feedback_repo, campaign_candidate_repo


def test_hands_each_ended_round_to_the_shared_pending_resolver():
    schedule = _schedule()
    interviewer = _interviewer()
    campaign_candidate = SimpleNamespace(id=schedule.campaign_candidate_id, candidate_id=uuid4())
    service, _isr, interview_feedback_repo, campaign_candidate_repo = _make_service(
        ended_rounds=[schedule], interviewers_by_schedule={schedule.id: [interviewer]},
        campaign_candidates_by_id={schedule.campaign_candidate_id: campaign_candidate},
    )

    with patch(f"{MODULE}.queue_pending_feedback_requests_for_round", return_value=1) as mock_resolve:
        count = service.run()

    assert count == 1
    mock_resolve.assert_called_once_with(
        service.db, campaign_candidate, schedule, [interviewer], interview_feedback_repo,
    )


def test_sums_the_resolver_return_value_across_multiple_rounds():
    schedule_a, schedule_b = _schedule(), _schedule()
    interviewer_a, interviewer_b = _interviewer(), _interviewer()
    service, *_repos = _make_service(
        ended_rounds=[schedule_a, schedule_b],
        interviewers_by_schedule={schedule_a.id: [interviewer_a], schedule_b.id: [interviewer_b]},
    )

    with patch(f"{MODULE}.queue_pending_feedback_requests_for_round", side_effect=[2, 0]) as mock_resolve:
        count = service.run()

    assert count == 2
    assert mock_resolve.call_count == 2


def test_a_round_with_no_interviewers_at_all_is_skipped_without_calling_the_resolver():
    schedule = _schedule()
    service, *_repos = _make_service(ended_rounds=[schedule], interviewers_by_schedule={schedule.id: []})

    with patch(f"{MODULE}.queue_pending_feedback_requests_for_round") as mock_resolve:
        count = service.run()

    assert count == 0
    mock_resolve.assert_not_called()


def test_a_round_whose_campaign_candidate_no_longer_exists_is_skipped_without_calling_the_resolver():
    schedule = _schedule()
    interviewer = _interviewer()
    service, _isr, _ifr, campaign_candidate_repo = _make_service(
        ended_rounds=[schedule], interviewers_by_schedule={schedule.id: [interviewer]},
    )
    campaign_candidate_repo.get_by_id.side_effect = None
    campaign_candidate_repo.get_by_id.return_value = None

    with patch(f"{MODULE}.queue_pending_feedback_requests_for_round") as mock_resolve:
        count = service.run()

    assert count == 0
    mock_resolve.assert_not_called()


def test_run_returns_zero_when_nothing_has_ended():
    service, *_repos = _make_service(ended_rounds=[])

    assert service.run() == 0
