from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
from app.models.interview import InterviewPlatform
from app.services.notifications import candidate_notification_emails as mod

"""
Epic 5 Step 2 - queue-and-dispatch functions for the 4 real candidate-
facing trigger events. Plain functions taking a raw `db` (MagicMock here,
matching this project's universal repository-test convention) rather
than a class - see the module's own docstring for why.
"""

MODULE = "app.services.notifications.candidate_notification_emails"


def _campaign_candidate(**overrides):
    defaults = dict(id=uuid4(), candidate_id=uuid4())
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _schedule(**overrides):
    defaults = dict(
        id=uuid4(), start_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc), platform=InterviewPlatform.TEAMS,
        timezone="UTC",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _interviewer(name="Priya Sharma"):
    return SimpleNamespace(name=name)


def _patched(dedup_rows=None):
    notification_repo = MagicMock()
    notification_repo.get_by_interview_schedule_id_and_trigger_event.return_value = dedup_rows or []
    notification_repo.get_by_campaign_candidate_id_and_trigger_event.return_value = dedup_rows or []
    notification_repo.create.side_effect = lambda n: n

    template_repo = MagicMock()
    template_repo.get_active_by_trigger_event.return_value = SimpleNamespace(id=uuid4())

    return notification_repo, template_repo


# ----------------------------------------------------------------------
# _interview_email_context
# ----------------------------------------------------------------------

def test_interview_email_context_formats_date_time_mode_and_interviewers():
    schedule = _schedule()
    context = mod._interview_email_context(schedule, [_interviewer("Alice"), _interviewer("Bob")])

    assert context["interview_date"] == "August 28, 2026"
    assert context["interview_time"] == "2:00 PM UTC"
    assert context["interview_mode"] == "Teams"
    assert context["interviewer_name"] == "Alice, Bob"


def test_interview_email_context_falls_back_when_no_interviewers():
    schedule = _schedule()
    assert mod._interview_email_context(schedule, [])["interviewer_name"] == "the hiring team"


def test_interview_email_context_returns_none_when_schedule_has_no_start_at():
    schedule = _schedule(start_at=None)
    assert mod._interview_email_context(schedule, []) is None


def test_interview_email_context_converts_a_non_utc_timezone_before_formatting():
    """
    Timezone-discrepancy fix: start_at is stored UTC now - this converts
    back to schedule.timezone before formatting, with the zone
    abbreviation appended, instead of the previous bare strftime on the
    raw UTC value with no indicator at all.
    """
    schedule = _schedule(
        start_at=datetime(2026, 8, 28, 8, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc),
        timezone="Asia/Kolkata",
    )
    context = mod._interview_email_context(schedule, [])

    assert context["interview_date"] == "August 28, 2026"
    assert context["interview_time"] == "2:00 PM IST"


# ----------------------------------------------------------------------
# queue_interview_scheduled_email
# ----------------------------------------------------------------------

def test_queue_interview_scheduled_email_creates_and_dispatches():
    notification_repo, template_repo = _patched()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        cc = _campaign_candidate()
        schedule = _schedule()

        mod.queue_interview_scheduled_email(MagicMock(), cc, schedule, [_interviewer()])

        notification_repo.create.assert_called_once()
        created = notification_repo.create.call_args.args[0]
        assert isinstance(created, EmailNotification)
        assert created.candidate_id == cc.candidate_id
        assert created.campaign_candidate_id == cc.id
        assert created.interview_schedule_id == schedule.id
        assert created.trigger_event == EmailTriggerEvent.INTERVIEW_SCHEDULED
        assert created.template_context["interview_date"] == "August 28, 2026"
        notification_repo.commit.assert_called_once()
        mock_task.apply_async.assert_called_once_with(kwargs={"email_notification_id": str(created.id)})


def test_queue_interview_scheduled_email_dedups_per_round_not_per_candidate():
    """
    A candidate can have many rounds - an existing QUEUED/SENT
    notification for round 1 must never block round 2's own
    INTERVIEW_SCHEDULED email. Scoped by interview_schedule_id, so this
    test's dedup rows (for THIS schedule.id) correctly block a repeat.
    """
    existing = SimpleNamespace(status=EmailNotificationStatus.SENT)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        mod.queue_interview_scheduled_email(MagicMock(), _campaign_candidate(), _schedule(), [])

        notification_repo.create.assert_not_called()
        mock_task.apply_async.assert_not_called()


def test_queue_interview_scheduled_email_skips_when_no_active_template():
    notification_repo, template_repo = _patched()
    template_repo.get_active_by_trigger_event.return_value = None
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        mod.queue_interview_scheduled_email(MagicMock(), _campaign_candidate(), _schedule(), [])

        notification_repo.create.assert_not_called()
        mock_task.apply_async.assert_not_called()


def test_queue_interview_scheduled_email_swallows_exceptions():
    """A failure to queue/dispatch must never propagate - the triggering write already committed."""
    notification_repo, template_repo = _patched()
    notification_repo.commit.side_effect = RuntimeError("db exploded")
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        mod.queue_interview_scheduled_email(MagicMock(), _campaign_candidate(), _schedule(), [])

        mock_task.apply_async.assert_not_called()


def test_queue_interview_scheduled_email_swallows_exceptions_from_the_dedup_lookup_itself():
    """
    Regression guard: an earlier version of this module ran the dedup
    lookup OUTSIDE _queue_and_dispatch's try/except (the caller did the
    lookup, then passed a plain bool in) - any failure there (a real DB
    error, a misbehaving mock in some existing caller's test fixture)
    would propagate straight out of queue_interview_scheduled_email into
    the real caller (interview_schedule_service.schedule()), defeating
    the entire "never propagate" guarantee this module exists to provide.
    Pinned directly here: a db whose query chain raises must still result
    in a fully swallowed failure, exactly like a failure during commit.
    """
    db = MagicMock()
    db.query.side_effect = RuntimeError("query blew up")
    with patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        mod.queue_interview_scheduled_email(db, _campaign_candidate(), _schedule(), [])

        mock_task.apply_async.assert_not_called()


# ----------------------------------------------------------------------
# queue_interview_rescheduled_email / queue_interview_cancelled_email -
# same shape, lighter coverage: prove each uses its own trigger_event
# and its own dedup scope.
# ----------------------------------------------------------------------

def test_queue_interview_rescheduled_email_uses_the_rescheduled_trigger_event():
    notification_repo, template_repo = _patched()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task"):
        schedule = _schedule()
        mod.queue_interview_rescheduled_email(MagicMock(), _campaign_candidate(), schedule, [])

        template_repo.get_active_by_trigger_event.assert_called_once_with(EmailTriggerEvent.INTERVIEW_RESCHEDULED)
        notification_repo.get_by_interview_schedule_id_and_trigger_event.assert_called_once_with(
            schedule.id, EmailTriggerEvent.INTERVIEW_RESCHEDULED,
        )
        created = notification_repo.create.call_args.args[0]
        assert created.trigger_event == EmailTriggerEvent.INTERVIEW_RESCHEDULED


def test_queue_interview_cancelled_email_uses_the_cancelled_trigger_event_and_prior_start_at():
    """cancel() never clears start_at, so the snapshot correctly captures the time that was actually cancelled."""
    notification_repo, template_repo = _patched()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task"):
        schedule = _schedule()
        mod.queue_interview_cancelled_email(MagicMock(), _campaign_candidate(), schedule, [])

        created = notification_repo.create.call_args.args[0]
        assert created.trigger_event == EmailTriggerEvent.INTERVIEW_CANCELLED
        assert created.template_context["interview_date"] == "August 28, 2026"


# ----------------------------------------------------------------------
# queue_candidate_selected_email - terminal, deduped per campaign_candidate.
# ----------------------------------------------------------------------

def test_queue_candidate_selected_email_creates_and_dispatches():
    notification_repo, template_repo = _patched()
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        cc = _campaign_candidate()

        mod.queue_candidate_selected_email(MagicMock(), cc)

        created = notification_repo.create.call_args.args[0]
        assert created.trigger_event == EmailTriggerEvent.CANDIDATE_SELECTED
        assert created.interview_schedule_id is None
        assert created.template_context is None
        notification_repo.get_by_campaign_candidate_id_and_trigger_event.assert_called_once_with(
            cc.id, EmailTriggerEvent.CANDIDATE_SELECTED,
        )
        mock_task.apply_async.assert_called_once()


def test_queue_candidate_selected_email_dedups_by_campaign_candidate_terminal_semantics():
    existing = SimpleNamespace(status=EmailNotificationStatus.QUEUED)
    notification_repo, template_repo = _patched(dedup_rows=[existing])
    with patch(f"{MODULE}.EmailNotificationRepository", return_value=notification_repo), \
         patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo), \
         patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        mod.queue_candidate_selected_email(MagicMock(), _campaign_candidate())

        notification_repo.create.assert_not_called()
        mock_task.apply_async.assert_not_called()
