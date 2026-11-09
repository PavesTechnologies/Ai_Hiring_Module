"""
Review item 7: every automatic queuer stamps its row with a dedup key, and the database's partial unique
index (uq_email_notifications_dedup_key) lets only one live row hold it - a racing duplicate is dropped,
never dispatched. Manual re-sends carry no key.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent, QUEUED_OR_DELIVERED
from app.services.notifications.dedup import notification_dedup_key


def test_key_shapes():
    cc, round_id, interviewer, event = uuid4(), uuid4(), uuid4(), uuid4()
    assert notification_dedup_key(EmailTriggerEvent.CANDIDATE_REJECTED, campaign_candidate_id=cc) == \
        f"CANDIDATE_REJECTED|cc={cc}"
    assert notification_dedup_key(EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED, interview_schedule_id=round_id,
                                  interviewer_id=interviewer) == \
        f"INTERVIEW_FEEDBACK_REQUESTED|round={round_id}|interviewer={interviewer}"
    assert notification_dedup_key(EmailTriggerEvent.INTERVIEW_RESCHEDULED, interview_schedule_id=round_id,
                                  event_id=event).endswith(f"|event={event}")
    with pytest.raises(ValueError):
        notification_dedup_key(EmailTriggerEvent.CANDIDATE_SELECTED)


def test_a_claimed_row_counts_as_already_notified():
    assert EmailNotificationStatus.SENDING in QUEUED_OR_DELIVERED


def test_the_model_declares_the_partial_unique_index():
    index = next(i for i in EmailNotification.__table__.indexes if i.name == "uq_email_notifications_dedup_key")
    assert index.unique and [c.name for c in index.columns] == ["dedup_key"]
    assert "status <> 'FAILED'" in str(index.dialect_options["postgresql"]["where"])


# -- each queuer: stamps the key; a duplicate (None from the insert) is never dispatched ------------

def _repo(duplicate: bool):
    repo = MagicMock()
    repo.get_by_campaign_candidate_id_and_trigger_event.return_value = []
    repo.get_by_interview_schedule_id_and_trigger_event.return_value = []
    repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event.return_value = []
    repo.create_unless_duplicate.side_effect = (lambda n: None) if duplicate else (lambda n: n)
    return repo


def _schedule():
    from datetime import datetime, timezone
    return SimpleNamespace(id=uuid4(), start_at=datetime(2026, 12, 1, 9, tzinfo=timezone.utc), timezone="UTC",
                           candidate_timezone=None, platform=None, meeting_link=None, location=None, notes=None)


def _interviewer():
    return SimpleNamespace(id=uuid4(), email="interviewer@example.invalid", name="Pat", timezone=None)


CASES = {
    "selected": ("app.services.notifications.candidate_notification_emails",
                 lambda mod, cc, s, i: mod.queue_candidate_selected_email(MagicMock(), cc),
                 lambda cc, s, i: f"CANDIDATE_SELECTED|cc={cc.id}"),
    "scheduled": ("app.services.notifications.candidate_notification_emails",
                  lambda mod, cc, s, i: mod.queue_interview_scheduled_email(MagicMock(), cc, s, [i]),
                  lambda cc, s, i: f"INTERVIEW_SCHEDULED|round={s.id}"),
    "invitation": ("app.services.notifications.interview_interviewer_lifecycle_emails",
                   lambda mod, cc, s, i: mod.queue_interview_interviewer_invitation_email(MagicMock(), cc, s, i),
                   lambda cc, s, i: f"INTERVIEW_INTERVIEWER_INVITATION|round={s.id}|interviewer={i.id}"),
    "feedback": ("app.services.notifications.interview_feedback_request_emails",
                 lambda mod, cc, s, i: mod.queue_interview_feedback_requested_email(MagicMock(), cc, s, i),
                 lambda cc, s, i: f"INTERVIEW_FEEDBACK_REQUESTED|round={s.id}|interviewer={i.id}"),
}


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("duplicate", [False, True])
def test_each_queuer_stamps_its_key_and_drops_a_racing_duplicate(case, duplicate):
    import importlib
    module_name, call, expected_key = CASES[case]
    mod = importlib.import_module(module_name)
    repo = _repo(duplicate)
    cc, schedule, interviewer = SimpleNamespace(id=uuid4(), candidate_id=uuid4()), _schedule(), _interviewer()
    with patch.object(mod, "EmailNotificationRepository", return_value=repo), \
            patch.object(mod, "EmailTemplateRepository", return_value=MagicMock()), \
            patch.object(mod, "send_candidate_email_task") as task:
        call(mod, cc, schedule, interviewer)
    created = repo.create_unless_duplicate.call_args.args[0]
    assert created.dedup_key == expected_key(cc, schedule, interviewer)
    assert task.apply_async.called is (not duplicate)


def test_a_manual_resend_carries_no_key():
    from app.services.notifications import candidate_notification_emails as mod
    repo = _repo(False)
    with patch.object(mod, "EmailNotificationRepository", return_value=repo), \
            patch.object(mod, "EmailTemplateRepository", return_value=MagicMock()), \
            patch.object(mod, "send_candidate_email_task"):
        mod.queue_candidate_selected_email(MagicMock(), SimpleNamespace(id=uuid4(), candidate_id=uuid4()),
                                           allow_resend=True)
    assert repo.create_unless_duplicate.call_args.args[0].dedup_key is None


@pytest.mark.parametrize("allow_resend, keyed", [(False, True), (True, False)])
def test_the_rejection_queuer_keys_only_automatic_sends(allow_resend, keyed):
    from app.services.notifications.candidate_rejection_email_service import CandidateRejectionEmailService
    repo = _repo(False)
    service = CandidateRejectionEmailService(MagicMock(), repo)
    cc = uuid4()
    service.queue_rejection_email(uuid4(), cc, allow_resend=allow_resend)
    key = repo.create_unless_duplicate.call_args.args[0].dedup_key
    assert key == (f"CANDIDATE_REJECTED|cc={cc}" if keyed else None)
