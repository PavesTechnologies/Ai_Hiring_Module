from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.services.notifications import interview_feedback_request_emails as mod

MODULE = "app.services.notifications.interview_feedback_request_emails"

"""
Epic 5 Step 4 - queue_interview_feedback_requested_email, the first
notification function in this codebase whose recipient is an
EXTERNAL_INTERVIEWER, not a candidate.
"""


def _campaign_candidate():
    return SimpleNamespace(id=uuid4(), candidate_id=uuid4())


def _schedule():
    return SimpleNamespace(id=uuid4())


def _interviewer():
    return SimpleNamespace(id=uuid4(), name="Priya Sharma", email="priya@example.com")


_NO_TEMPLATE_OVERRIDE = object()


def _patched(dedup_rows=None, template=_NO_TEMPLATE_OVERRIDE):
    notification_repo = MagicMock()
    notification_repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event.return_value = dedup_rows or []
    notification_repo.create.side_effect = lambda n: n

    template_repo = MagicMock()
    template_repo.get_active_by_trigger_event.return_value = (
        SimpleNamespace(id=uuid4()) if template is _NO_TEMPLATE_OVERRIDE else template
    )

    return notification_repo, template_repo


def test_queues_and_dispatches_with_a_real_signed_token_in_the_link():
    notification_repo, template_repo = _patched()
    cc, schedule, interviewer = _campaign_candidate(), _schedule(), _interviewer()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task, \
         patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.frontend_base_url = "https://app.example.com"

        result = mod.queue_interview_feedback_requested_email(MagicMock(), cc, schedule, interviewer)

    assert result is True
    created = notification_repo.create.call_args.args[0]
    assert isinstance(created, EmailNotification)
    assert created.recipient_type == EmailRecipientType.EXTERNAL_INTERVIEWER
    assert created.candidate_id is None
    assert created.campaign_candidate_id == cc.id
    assert created.interview_schedule_id == schedule.id
    assert created.interview_interviewer_id == interviewer.id
    assert created.recipient_email == interviewer.email
    assert created.recipient_name == interviewer.name
    assert created.trigger_event == EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED
    assert created.template_context["recipient_name"] == interviewer.name
    assert created.template_context["feedback_link"].startswith("https://app.example.com/interview-feedback/")
    notification_repo.commit.assert_called_once()
    mock_task.apply_async.assert_called_once_with(kwargs={"email_notification_id": str(created.id)})


def test_dedups_per_interviewer_not_per_round():
    """Two interviewers on the same round - interviewer A's existing email must not block interviewer B's."""
    existing = SimpleNamespace(status=EmailNotificationStatus.SENT)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_feedback_requested_email(MagicMock(), _campaign_candidate(), _schedule(), _interviewer())

    assert result is False
    notification_repo.create.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_skips_when_no_active_template():
    notification_repo, template_repo = _patched(template=None)
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_feedback_requested_email(MagicMock(), _campaign_candidate(), _schedule(), _interviewer())

    assert result is False
    notification_repo.create.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_swallows_exceptions_and_returns_false():
    notification_repo, template_repo = _patched()
    notification_repo.commit.side_effect = RuntimeError("db exploded")
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_feedback_requested_email(MagicMock(), _campaign_candidate(), _schedule(), _interviewer())

    assert result is False
    mock_task.apply_async.assert_not_called()


def test_swallows_exceptions_from_the_dedup_lookup_itself():
    """Same regression class as candidate_notification_emails.py - everything must be inside the try/except, including the dedup query."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("query blew up")
    with patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_feedback_requested_email(db, _campaign_candidate(), _schedule(), _interviewer())

    assert result is False
    mock_task.apply_async.assert_not_called()


# ----------------------------------------------------------------------
# queue_pending_feedback_requests_for_round - shared "who still needs
# asking" resolution, used by both the hourly sweep and the manual
# "Request Feedback" action, so it's tested once here rather than
# separately (and possibly divergently) by each caller.
# ----------------------------------------------------------------------

def _feedback_repo(rows):
    repo = MagicMock()
    repo.get_by_interview_schedule_id.return_value = rows
    return repo


def test_queues_every_interviewer_when_none_have_given_feedback():
    schedule = _schedule()
    a, b = _interviewer(), _interviewer()
    with patch(f"{MODULE}.queue_interview_feedback_requested_email", return_value=True) as mock_queue:
        count = mod.queue_pending_feedback_requests_for_round(
            MagicMock(), _campaign_candidate(), schedule, [a, b], _feedback_repo([]),
        )

    assert count == 2
    assert mock_queue.call_count == 2


def test_excludes_an_interviewer_who_already_gave_feedback():
    schedule = _schedule()
    fed_back, pending = _interviewer(), _interviewer()
    feedback_repo = _feedback_repo([SimpleNamespace(interviewer_id=fed_back.id)])
    with patch(f"{MODULE}.queue_interview_feedback_requested_email", return_value=True) as mock_queue:
        count = mod.queue_pending_feedback_requests_for_round(
            MagicMock(), _campaign_candidate(), schedule, [fed_back, pending], feedback_repo,
        )

    assert count == 1
    mock_queue.assert_called_once()
    assert mock_queue.call_args.args[3] is pending


def test_does_not_count_a_duplicate_the_leaf_function_itself_rejected():
    """The interviewer isn't excluded by the feedback check, but queue_interview_feedback_requested_email's own dedup returns False (already emailed) - must not be counted."""
    schedule = _schedule()
    interviewer = _interviewer()
    with patch(f"{MODULE}.queue_interview_feedback_requested_email", return_value=False) as mock_queue:
        count = mod.queue_pending_feedback_requests_for_round(
            MagicMock(), _campaign_candidate(), schedule, [interviewer], _feedback_repo([]),
        )

    assert count == 0
    mock_queue.assert_called_once()


def test_returns_zero_for_an_empty_interviewer_list():
    count = mod.queue_pending_feedback_requests_for_round(
        MagicMock(), _campaign_candidate(), _schedule(), [], _feedback_repo([]),
    )
    assert count == 0
