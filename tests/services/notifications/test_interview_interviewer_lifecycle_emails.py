from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.services.notifications import interview_interviewer_lifecycle_emails as mod

MODULE = "app.services.notifications.interview_interviewer_lifecycle_emails"

"""
Interviewer lifecycle follow-up - the 3 new EXTERNAL_INTERVIEWER-recipient
trigger events (invitation, removal notice, cancellation notice), same
shape as queue_interview_feedback_requested_email
(test_interview_feedback_request_emails.py is the direct precedent this
file mirrors).
"""


def _campaign_candidate():
    return SimpleNamespace(id=uuid4(), candidate_id=uuid4())


def _schedule(**overrides):
    from datetime import datetime, timezone
    defaults = dict(
        id=uuid4(), start_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), platform=None, notes=None,
        timezone="UTC",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


# ----------------------------------------------------------------------
# queue_interview_interviewer_invitation_email
# ----------------------------------------------------------------------

def test_invitation_queues_and_dispatches_with_round_context():
    notification_repo, template_repo = _patched()
    cc, schedule, interviewer = _campaign_candidate(), _schedule(notes="Bring laptop"), _interviewer()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_invitation_email(MagicMock(), cc, schedule, interviewer)

    assert result is True
    created = notification_repo.create.call_args.args[0]
    assert isinstance(created, EmailNotification)
    assert created.recipient_type == EmailRecipientType.EXTERNAL_INTERVIEWER
    assert created.trigger_event == EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION
    assert created.interview_interviewer_id == interviewer.id
    assert created.template_context["recipient_name"] == interviewer.name
    assert created.template_context["notes_block"] == "\n\nNotes: Bring laptop"
    assert "interview_date" in created.template_context
    notification_repo.commit.assert_called_once()
    mock_task.apply_async.assert_called_once_with(kwargs={"email_notification_id": str(created.id)})


def test_invitation_notes_block_is_empty_when_notes_absent():
    notification_repo, template_repo = _patched()
    schedule = _schedule(notes=None)
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task"):
        mod.queue_interview_interviewer_invitation_email(MagicMock(), _campaign_candidate(), schedule, _interviewer())

    created = notification_repo.create.call_args.args[0]
    assert created.template_context["notes_block"] == ""


def test_round_context_converts_a_non_utc_timezone_before_formatting():
    """
    Timezone-discrepancy fix: start_at is stored UTC now - _round_context
    converts back to schedule.timezone before formatting, with the zone
    abbreviation appended, instead of the previous bare strftime on the
    raw UTC value with no timezone indicator at all.
    """
    from datetime import datetime, timezone as tz
    schedule = _schedule(start_at=datetime(2026, 8, 25, 9, 30, tzinfo=tz.utc), timezone="Asia/Kolkata")

    context = mod._round_context(schedule)

    assert context["interview_date"] == "August 25, 2026"
    assert context["interview_time"] == "3:00 PM IST"


def test_round_context_shows_tbd_when_schedule_has_no_start_at():
    schedule = _schedule(start_at=None)

    context = mod._round_context(schedule)

    assert context["interview_date"] == "TBD"
    assert context["interview_time"] == "TBD"


def test_invitation_dedups_per_interviewer_per_round():
    existing = SimpleNamespace(status=EmailNotificationStatus.SENT)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_invitation_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    notification_repo.create.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_invitation_skips_when_no_active_template():
    notification_repo, template_repo = _patched(template=None)
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_invitation_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    mock_task.apply_async.assert_not_called()


def test_invitation_swallows_exceptions_and_returns_false():
    notification_repo, template_repo = _patched()
    notification_repo.commit.side_effect = RuntimeError("db exploded")
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_invitation_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    mock_task.apply_async.assert_not_called()


# ----------------------------------------------------------------------
# queue_interview_interviewer_removed_email
# ----------------------------------------------------------------------

def test_removed_queues_with_minimal_context_no_meeting_details():
    notification_repo, template_repo = _patched()
    cc, schedule, interviewer = _campaign_candidate(), _schedule(), _interviewer()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_removed_email(MagicMock(), cc, schedule, interviewer)

    assert result is True
    created = notification_repo.create.call_args.args[0]
    assert created.trigger_event == EmailTriggerEvent.INTERVIEW_INTERVIEWER_REMOVED
    assert created.template_context == {"recipient_name": interviewer.name}
    mock_task.apply_async.assert_called_once()


def test_removed_dedups_per_interviewer_per_round():
    existing = SimpleNamespace(status=EmailNotificationStatus.QUEUED)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_removed_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    mock_task.apply_async.assert_not_called()


# ----------------------------------------------------------------------
# queue_interview_interviewer_cancelled_email
# ----------------------------------------------------------------------

def test_cancelled_includes_reason_when_given():
    notification_repo, template_repo = _patched()
    cc, schedule, interviewer = _campaign_candidate(), _schedule(), _interviewer()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_cancelled_email(
            MagicMock(), cc, schedule, interviewer, "Candidate withdrew",
        )

    assert result is True
    created = notification_repo.create.call_args.args[0]
    assert created.trigger_event == EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED
    assert created.template_context["reason_block"] == "\n\nReason: Candidate withdrew"
    mock_task.apply_async.assert_called_once()


def test_cancelled_reason_block_is_empty_when_reason_absent():
    notification_repo, template_repo = _patched()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task"):
        mod.queue_interview_interviewer_cancelled_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    created = notification_repo.create.call_args.args[0]
    assert created.template_context["reason_block"] == ""


def test_cancelled_dedups_per_interviewer_per_round():
    existing = SimpleNamespace(status=EmailNotificationStatus.SENT)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_cancelled_email(
            MagicMock(), _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    mock_task.apply_async.assert_not_called()


def test_swallows_exceptions_from_the_dedup_lookup_itself():
    """Same regression class as candidate_notification_emails.py - everything must be inside the try/except, including the dedup query."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("query blew up")
    with patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = mod.queue_interview_interviewer_invitation_email(
            db, _campaign_candidate(), _schedule(), _interviewer(),
        )

    assert result is False
    mock_task.apply_async.assert_not_called()
