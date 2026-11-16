"""
Against Postgres (rolled back): email drafts (email plan, phase 3) - nothing goes to a candidate or an interviewer
until a person previews the email and clicks Send. Rows are cloned from dev's synthetic data inside the test's
transaction; dispatching is patched (no task reaches Redis) and no email is sent.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.exceptions.campaign_exceptions import CampaignException
from app.models.compliance import AuditLog
from app.models.email import EmailNotification, EmailNotificationStatus as S, EmailTemplate, EmailTriggerEvent as E
from app.models.interview import InterviewStatus
from app.models.pipeline import PipelineStage
from app.services.notifications import drafts
from tests.db.test_send_policy_db import (  # noqa: F401 - fixtures
    NOW, _interview_email, _round, _saved, _service, _template, db, world,
)

HR = SimpleNamespace(user_id="zz-hr", roles=["HR_ADMIN"], authorised_roles=())
OUTSIDER = SimpleNamespace(user_id="zz-other-hm", roles=["HIRING_MANAGER"], authorised_roles=())


@pytest.fixture
def dispatch():
    with patch("app.tasks.email_tasks.send_candidate_email_task.apply_async") as task:
        yield task


def _audit(world):
    from app.repositories.audit_repository import AuditRepository
    from app.services.audit_service import AuditService
    return AuditService(AuditRepository(world.db))


def _rejection_draft(world):
    result = _service(world).send_rejection_email(world.cc.id, actor_id="zz-hr", actor_roles=["HR_ADMIN"])
    assert result.status == "draft"
    return world.db.get(EmailNotification, result.draft_id)


def test_preparing_a_rejection_makes_a_draft_and_sends_nothing(world, dispatch):
    draft = _rejection_draft(world)
    assert draft.status == S.DRAFT and draft.prepared_by == "zz-hr" and draft.prepared_at is not None
    dispatch.assert_not_called()


def test_the_preview_shows_the_rendered_email_and_its_recipient(world, dispatch):
    row, preview = drafts.get_preview(world.db, _rejection_draft(world).id, actor=HR)
    assert preview.problems == [] and preview.subject and preview.body
    assert preview.recipient_address and "@" in preview.recipient_address and preview.recipient_name
    assert len(preview.preview_hash) == 64


def test_send_needs_the_previewed_email_then_queues_once(world, dispatch):
    draft = _rejection_draft(world)
    _, preview = drafts.get_preview(world.db, draft.id, actor=HR)
    with pytest.raises(drafts.DraftError) as stale:
        drafts.send(world.db, draft.id, actor=HR, preview_hash="0" * 64, audit_service=_audit(world))
    assert stale.value.status_code == 409 and stale.value.data["code"] == "PREVIEW_CHANGED"
    dispatch.assert_not_called()

    sent = drafts.send(world.db, draft.id, actor=HR, preview_hash=preview.preview_hash, audit_service=_audit(world))
    assert sent.status == S.QUEUED and sent.confirmed_by == "zz-hr" and sent.confirmed_at is not None
    dispatch.assert_called_once_with(kwargs={"email_notification_id": str(draft.id)})

    with pytest.raises(drafts.DraftError) as twice:                       # a double click / a second tab
        drafts.send(world.db, draft.id, actor=HR, preview_hash=preview.preview_hash, audit_service=_audit(world))
    assert twice.value.status_code == 409 and dispatch.call_count == 1
    audited = world.db.execute(select(AuditLog).where(AuditLog.entity_id == draft.id)).scalars().all()
    assert [a.action_type.value for a in audited] == ["EMAIL_DRAFT_SENT"]
    assert "@" not in str(audited[0].detail)                                # ids and codes only


def test_send_refuses_when_the_checks_fail_at_send(world, dispatch):
    draft = _rejection_draft(world)
    _, preview = drafts.get_preview(world.db, draft.id, actor=HR)
    world.candidate.erasure_requested_at = NOW
    world.db.flush()
    with pytest.raises(drafts.DraftError) as refused:
        drafts.send(world.db, draft.id, actor=HR, preview_hash=preview.preview_hash, audit_service=_audit(world))
    assert refused.value.status_code == 422
    assert "ERASURE_REQUESTED" in {p["code"] for p in refused.value.data["problems"]}
    world.db.refresh(draft)
    assert draft.status == S.DRAFT and not dispatch.called


def test_discard_is_audited_and_a_new_draft_can_be_prepared(world, dispatch):
    draft = _rejection_draft(world)
    drafts.discard(world.db, draft.id, actor=HR, audit_service=_audit(world))
    world.db.refresh(draft)
    assert draft.status == S.DISCARDED
    assert _rejection_draft(world).id != draft.id                         # DISCARDED no longer holds the key
    with pytest.raises(drafts.DraftError):
        drafts.discard(world.db, draft.id, actor=HR, audit_service=_audit(world))
    dispatch.assert_not_called()


def test_someone_without_campaign_access_can_neither_preview_nor_send(world, dispatch):
    draft = _rejection_draft(world)
    for attempt in (lambda: drafts.get_preview(world.db, draft.id, actor=OUTSIDER),
                    lambda: drafts.send(world.db, draft.id, actor=OUTSIDER, preview_hash="x", audit_service=_audit(world))):
        with pytest.raises(drafts.DraftError) as denied:
            attempt()
        assert denied.value.status_code == 403
    dispatch.assert_not_called()


def test_a_draft_uses_the_template_that_replaced_its_own(world, dispatch):
    draft = _rejection_draft(world)
    old = _template(world.db, E.CANDIDATE_REJECTED)
    old.is_active = False
    world.db.flush()
    new = EmailTemplate(id=uuid4(), trigger_event=E.CANDIDATE_REJECTED, name="zz new", subject="Update for {candidate_name}",
                        body_template="Dear {candidate_name}, about {job_title}.", is_active=True)
    world.db.add(new)
    world.db.flush()
    _, preview = drafts.get_preview(world.db, draft.id, actor=HR)
    assert draft.template_id == new.id and preview.subject.startswith("Update for") and preview.problems == []


def test_the_send_task_never_sends_a_draft(world, dispatch):
    from app.tasks.email_tasks import send_candidate_email_task
    draft = _rejection_draft(world)
    with patch("app.tasks.email_tasks.SessionLocal", return_value=world.db), patch.object(world.db, "close"), \
            patch("app.tasks.email_tasks.SESEmailClient", side_effect=AssertionError("the sender was called")):
        send_candidate_email_task.apply(kwargs={"email_notification_id": str(draft.id)})
    world.db.refresh(draft)
    assert draft.status == S.DRAFT


# --- rounds: a newer state replaces an unsent draft -----------------------------------------------

def test_an_unsent_round_draft_is_replaced_and_a_cancellation_needs_someone_told(world, dispatch):
    from app.services.notifications.candidate_notification_emails import queue_interview_scheduled_email
    world.cc.pipeline_stage = PipelineStage.INTERVIEW
    world.db.flush()
    schedule, people = _round(world)
    first = queue_interview_scheduled_email(world.db, world.cc, schedule, people, prepared_by="zz-hr")
    assert first is not None and first.status == S.DRAFT
    assert drafts.told_about_round(world.db, schedule.id) is False          # a draft tells nobody
    assert drafts.discard_round_drafts(world.db, schedule.id, candidate=True, triggers=drafts.CANDIDATE_ROUND_EVENTS) == 1
    world.db.refresh(first)
    assert first.status == S.DISCARDED
    second = queue_interview_scheduled_email(world.db, world.cc, schedule, people, prepared_by="zz-hr")
    assert second is not None and second.id != first.id                    # the key is free again
    _saved(world, _interview_email(world, E.INTERVIEW_SCHEDULED, schedule))  # once one was actually sent...
    assert drafts.told_about_round(world.db, schedule.id) is True
    dispatch.assert_not_called()


def test_the_feedback_sweep_doesnt_prepare_again_what_a_person_discarded(world, dispatch):
    from app.services.notifications.interview_feedback_request_emails import queue_interview_feedback_requested_email
    schedule, (person,) = _round(world, start=NOW - timedelta(hours=3), status=InterviewStatus.COMPLETED)
    assert queue_interview_feedback_requested_email(world.db, world.cc, schedule, person) is True
    row = world.db.execute(select(EmailNotification).where(
        EmailNotification.interview_schedule_id == schedule.id)).scalars().one()
    assert row.status == S.DRAFT
    drafts.discard(world.db, row.id, actor=HR, audit_service=_audit(world))
    assert queue_interview_feedback_requested_email(world.db, world.cc, schedule, person) is False     # the sweep
    assert queue_interview_feedback_requested_email(world.db, world.cc, schedule, person, manual=True) is True
    dispatch.assert_not_called()


# --- bulk: the confirm step is the check ---------------------------------------------------------

def test_the_bulk_send_queues_exactly_the_checked_list(world, dispatch):
    service = _service(world)
    check = service.check_bulk_rejection_email([world.cc.id, world.cc.id], actor_id="zz-hr", actor_roles=["HR_ADMIN"])
    assert [e["campaign_candidate_id"] for e in check.will_receive] == [str(world.cc.id)]
    with pytest.raises(CampaignException) as stale:
        service.bulk_send_rejection_email([world.cc.id], actor_id="zz-hr", actor_roles=["HR_ADMIN"], check_hash="x",
                                          audit_service=_audit(world))
    assert stale.value.status_code == 409
    result = service.bulk_send_rejection_email([world.cc.id, world.cc.id], actor_id="zz-hr", actor_roles=["HR_ADMIN"],
                                               check_hash=check.check_hash, audit_service=_audit(world))
    assert result.queued == [world.cc.id] and result.repeated == 1
    row = world.db.execute(select(EmailNotification).where(
        EmailNotification.campaign_candidate_id == world.cc.id)).scalars().one()
    assert row.status == S.QUEUED and row.confirmed_by == "zz-hr"
    dispatch.assert_called_once()


def test_the_bulk_preview_renders_one_candidates_email_and_creates_nothing(world, dispatch):
    service = _service(world)
    preview = service.preview_bulk_rejection_email(world.cc.id, actor_id="zz-hr", actor_roles=["HR_ADMIN"])
    assert preview["subject"] and preview["body"] and "@" in (preview["recipient_address"] or "")
    assert preview["problems"] == []
    assert world.db.execute(select(EmailNotification).where(
        EmailNotification.campaign_candidate_id == world.cc.id)).scalars().all() == []
    with pytest.raises(CampaignException) as denied:
        service.preview_bulk_rejection_email(world.cc.id, actor_id="zz-other-hm", actor_roles=["HIRING_MANAGER"])
    assert denied.value.status_code == 403
    dispatch.assert_not_called()
