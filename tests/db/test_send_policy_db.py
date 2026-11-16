"""
Against Postgres (rolled back): the checks before every email (app/services/notifications/send_policy.py) -
every rule, and that every send path enforces them (queueing, the manual sends, the send task).

Rows are cloned from dev's synthetic data inside the test's transaction; no task is dispatched and no email
is sent (EMAIL_DELIVERY=log, the sender is replaced by one that fails the test).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

import app.main  # noqa: F401 - registers every model
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.compliance import CandidateConsent
from app.models.email import (
    EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTemplate, EmailTriggerEvent as E,
)
from app.models.interview import (
    InterviewFeedback, InterviewFeedbackRecommendation, InterviewInterviewer, InterviewPlatform, InterviewSchedule,
    InterviewStatus,
)
from app.models.pipeline import CampaignCandidate, PipelineStage
from app.services.notifications import send_policy
from app.services.notifications.dedup import notification_dedup_key
from tests.db.test_campaign_candidate_delete_scored import _clone, db  # noqa: F401 - fixture

NOW = datetime.now(timezone.utc)


def _codes(found):
    return {p.code for p in found}


@pytest.fixture
def world(db):  # noqa: F811
    """A cloned active campaign with one candidate (consent given), its resume and campaign candidate."""
    source_resume = db.execute(select(Resume).limit(1)).scalar_one_or_none()
    source_campaign = db.execute(select(HiringCampaign).limit(1)).scalar_one_or_none()
    if source_resume is None or source_campaign is None:
        pytest.skip("needs a campaign and a resume to clone from")
    tag = uuid4().hex
    campaign = _clone(db, source_campaign, name=f"zz send policy {tag}", status=CampaignStatus.ACTIVE,
                      hiring_manager_id="zz-hm-owner")
    candidate = _clone(db, db.get(Candidate, source_resume.candidate_id), email_hash=f"zz-send-policy-{tag}",
                       erasure_requested_at=None, erasure_completed_at=None, consent_given=True)
    resume = _clone(db, source_resume, candidate_id=candidate.id, file_path=f"zz/send-policy/{tag}.docx",
                    file_hash=tag, bulk_upload_job_id=None)
    # Synthetic people for the actors the tests use (prepared_by / confirmed_by reference users).
    from app.models.identity import User, UserRole
    for user_id, role in (("hr", "HR_ADMIN"), ("zz-hr", "HR_ADMIN"), ("zz-hm-owner", "HIRING_MANAGER"),
                          ("zz-other-hm", "HIRING_MANAGER")):
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, email=f"{user_id}-{tag[:8]}@example.invalid", role=UserRole[role],
                        full_name=f"Test {user_id}", password_hash="EXTERNAL_AUTH", is_active=True))
    db.flush()
    cc = CampaignCandidate(id=uuid4(), campaign_id=campaign.id, candidate_id=candidate.id, resume_id=resume.id,
                           idempotency_key=f"zz-send-policy-{tag}", pipeline_stage=PipelineStage.REJECTED,
                           is_fraud_flagged=False)
    db.add(cc)
    db.flush()
    return SimpleNamespace(db=db, campaign=campaign, candidate=candidate, cc=cc)


def _template(db, event):  # noqa: F811
    template = db.execute(select(EmailTemplate).where(EmailTemplate.trigger_event == event,
                                                      EmailTemplate.is_active.is_(True))).scalar_one_or_none()
    if template is None:
        pytest.skip(f"no active {event.value} template in this database")
    return template


def _outcome(world, event, **overrides):
    template_id = overrides.pop("template_id") if "template_id" in overrides else _template(world.db, event).id
    values = dict(candidate_id=world.candidate.id, campaign_candidate_id=world.cc.id,
                  recipient_type=EmailRecipientType.CANDIDATE, trigger_event=event,
                  template_id=template_id, status=EmailNotificationStatus.QUEUED,
                  dedup_key=notification_dedup_key(event, campaign_candidate_id=world.cc.id))
    values.update(overrides)
    return EmailNotification(**values)


def _saved(world, notification, status=EmailNotificationStatus.SENT):
    notification.status = status
    notification.sent_at = NOW
    world.db.add(notification)
    world.db.flush()
    return notification


def _round(world, *, start=NOW + timedelta(days=2), status=InterviewStatus.SCHEDULED, interviewers=1, active=True):
    number = 1 + world.db.query(InterviewSchedule).filter(InterviewSchedule.campaign_candidate_id == world.cc.id).count()
    schedule = InterviewSchedule(id=uuid4(), campaign_candidate_id=world.cc.id, round_number=number, status=status,
                                 start_at=start, end_at=start + timedelta(hours=1) if start else None,
                                 timezone="UTC", platform=InterviewPlatform.PHONE)
    world.db.add(schedule)
    world.db.flush()
    people = []
    for i in range(interviewers):
        person = InterviewInterviewer(id=uuid4(), interview_id=schedule.id, name=f"Interviewer {i}",
                                      email=f"interviewer{i}@example.invalid", is_active=active)
        world.db.add(person)
        people.append(person)
    world.db.flush()
    return schedule, people


def _interview_email(world, event, schedule, *, when=("January 05, 2027", "10:00 AM UTC")):
    return EmailNotification(
        candidate_id=world.candidate.id, campaign_candidate_id=world.cc.id, interview_schedule_id=schedule.id,
        recipient_type=EmailRecipientType.CANDIDATE, trigger_event=event, template_id=_template(world.db, event).id,
        status=EmailNotificationStatus.QUEUED,
        template_context={"interview_date": when[0], "interview_time": when[1], "interview_mode": "Phone",
                          "interviewer_name": "Interviewer 0", "meeting_info": ""})


def _interviewer_email(world, event, schedule, person, context=None):
    return EmailNotification(
        campaign_candidate_id=world.cc.id, interview_schedule_id=schedule.id, interview_interviewer_id=person.id,
        recipient_type=EmailRecipientType.EXTERNAL_INTERVIEWER, recipient_email=person.email,
        recipient_name=person.name, trigger_event=event, template_id=_template(world.db, event).id,
        status=EmailNotificationStatus.QUEUED,
        template_context={"recipient_name": person.name, "feedback_link": "https://x.invalid/f",
                          "interview_date": "January 05, 2027", "interview_time": "10:00 AM UTC",
                          "interview_mode": "Phone", "notes_block": "", "reason_block": "", **(context or {})})


# --- every email ---------------------------------------------------------------------------------

def test_a_rejection_for_a_rejected_candidate_passes(world):
    assert send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED)) == []


@pytest.mark.parametrize("change, code", [
    (lambda w: setattr(w.candidate, "erasure_requested_at", NOW), "ERASURE_REQUESTED"),
    (lambda w: setattr(w.candidate, "erasure_completed_at", NOW), "CANDIDATE_ERASED"),
])
def test_erasure_blocks_every_email_about_the_candidate(world, change, code):
    change(world)
    world.db.flush()
    assert code in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED)))


def test_withdrawn_consent_blocks_the_email(world):
    world.db.add(CandidateConsent(candidate_id=world.candidate.id, consent_given=True, jurisdiction="IN",
                                  consent_timestamp=NOW - timedelta(days=1), consent_source="test",
                                  revoked_at=NOW))
    world.db.flush()
    assert "CONSENT_WITHDRAWN" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED)))


def test_an_invalid_interviewer_address_is_refused(world):
    schedule, (person,) = _round(world, start=NOW - timedelta(hours=3), status=InterviewStatus.COMPLETED)
    person.email = "not-an-address"
    email = _interviewer_email(world, E.INTERVIEW_FEEDBACK_REQUESTED, schedule, person)
    email.recipient_email = "not-an-address"
    assert "INVALID_RECIPIENT" in _codes(send_policy.problems(world.db, email))


@pytest.mark.parametrize("roles, user, code", [
    (["RECRUITER"], "zz-recruiter", "SENDER_NOT_ALLOWED"),
    (["HIRING_MANAGER"], "zz-another-hm", "NO_CAMPAIGN_ACCESS"),
])
def test_the_sender_needs_an_allowed_role_and_campaign_access(world, roles, user, code):
    actor = SimpleNamespace(user_id=user, roles=roles, authorised_roles=())
    assert code in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED), actor=actor))
    owner = SimpleNamespace(user_id="zz-hm-owner", roles=["HIRING_MANAGER"], authorised_roles=())
    assert send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED), actor=owner) == []


def test_a_template_that_cannot_render_completely_is_refused(world):
    active = _template(world.db, E.CANDIDATE_REJECTED)
    active.is_active = False
    world.db.flush()
    world.db.add(EmailTemplate(id=uuid4(), trigger_event=E.CANDIDATE_REJECTED, name="zz broken", subject="Hi",
                               body_template="Dear {candidate_name}, {not_a_value}", is_active=True))
    world.db.flush()
    email = _outcome(world, E.CANDIDATE_REJECTED, template_id=None)
    assert "TEMPLATE_INCOMPLETE" in _codes(send_policy.problems(world.db, email))


def test_no_active_template_is_refused(world):
    _template(world.db, E.CANDIDATE_REJECTED).is_active = False
    world.db.flush()
    email = _outcome(world, E.CANDIDATE_REJECTED, template_id=None)
    assert "NO_ACTIVE_TEMPLATE" in _codes(send_policy.problems(world.db, email))


def test_a_duplicate_is_refused(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=EmailNotificationStatus.QUEUED)
    assert "DUPLICATE" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED)))


# --- rejection and selection ----------------------------------------------------------------------

def test_rejection_only_for_rejected_and_selection_only_for_selected(world):
    assert "WRONG_STAGE" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_SELECTED)))
    world.cc.pipeline_stage = PipelineStage.SELECTED
    world.db.flush()
    assert "WRONG_STAGE" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED)))
    assert send_policy.problems(world.db, _outcome(world, E.CANDIDATE_SELECTED)) == []


def test_never_twice_and_never_both(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED))
    assert "ALREADY_SENT" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_REJECTED, dedup_key=None)))
    world.cc.pipeline_stage = PipelineStage.SELECTED
    world.db.flush()
    assert "OTHER_OUTCOME_SENT" in _codes(send_policy.problems(world.db, _outcome(world, E.CANDIDATE_SELECTED)))


# --- interview emails -----------------------------------------------------------------------------

def _interview_world(world):
    world.cc.pipeline_stage = PipelineStage.INTERVIEW
    world.db.flush()


def test_interview_scheduled_passes_with_everything_in_place(world):
    _interview_world(world)
    schedule, _ = _round(world)
    assert send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_SCHEDULED, schedule)) == []


@pytest.mark.parametrize("setup, code", [
    (lambda w, s: None, "NO_INTERVIEWERS"),
])
def test_interview_scheduled_needs_interviewers(world, setup, code):
    _interview_world(world)
    schedule, _ = _round(world, interviewers=0)
    assert code in _codes(send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_SCHEDULED, schedule)))


def test_interview_scheduled_needs_a_time_the_stage_an_active_campaign_and_no_outcome_email(world):
    schedule, _ = _round(world, start=None)
    world.campaign.status = CampaignStatus.PAUSED
    sent = _saved(world, _outcome(world, E.CANDIDATE_REJECTED))
    from app.services.campaign.stage_lock import lock_after_send       # as the send task does (phase 4)
    lock_after_send(world.db, sent)
    world.db.flush()
    found = _codes(send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_SCHEDULED, schedule)))
    assert {"NO_DATE_TIME", "NOT_IN_INTERVIEW_STAGE", "CAMPAIGN_NOT_ACTIVE", "CANDIDATE_LOCKED"} <= found


def test_a_reschedule_needs_a_real_change_from_what_the_candidate_was_told(world):
    _interview_world(world)
    schedule, _ = _round(world)
    _saved(world, _interview_email(world, E.INTERVIEW_SCHEDULED, schedule))
    same = _interview_email(world, E.INTERVIEW_RESCHEDULED, schedule)
    assert "NO_TIME_CHANGE" in _codes(send_policy.problems(world.db, same))
    moved = _interview_email(world, E.INTERVIEW_RESCHEDULED, schedule, when=("January 06, 2027", "10:00 AM UTC"))
    assert send_policy.problems(world.db, moved) == []


def test_a_reschedule_after_a_cancellation_email_counts_as_a_change(world):
    _interview_world(world)
    schedule, _ = _round(world)
    now = datetime.now(timezone.utc)                           # each email is its own transaction in real use
    scheduled = _interview_email(world, E.INTERVIEW_SCHEDULED, schedule)
    scheduled.created_at = now - timedelta(minutes=2)
    _saved(world, scheduled)
    cancelled = _interview_email(world, E.INTERVIEW_CANCELLED, schedule)
    cancelled.created_at = now - timedelta(minutes=1)
    _saved(world, cancelled)
    assert send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_RESCHEDULED, schedule)) == []


def test_a_reschedule_on_a_cancelled_round_is_refused(world):
    _interview_world(world)
    schedule, _ = _round(world, status=InterviewStatus.CANCELLED)
    assert "ROUND_CANCELLED" in _codes(send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_RESCHEDULED, schedule)))


def test_a_cancellation_needs_a_scheduled_round_that_is_cancelled(world):
    _interview_world(world)
    live, _ = _round(world)
    assert "ROUND_NOT_CANCELLED" in _codes(send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_CANCELLED, live)))
    cancelled, _ = _round(world, status=InterviewStatus.CANCELLED)
    assert send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_CANCELLED, cancelled)) == []
    never, _ = _round(world, start=None, status=InterviewStatus.CANCELLED)
    assert "NO_DATE_TIME" in _codes(send_policy.problems(world.db, _interview_email(world, E.INTERVIEW_CANCELLED, never)))


# --- feedback requests ----------------------------------------------------------------------------

def test_a_feedback_request_needs_an_ended_round(world):
    schedule, (person,) = _round(world)
    found = send_policy.problems(world.db, _interviewer_email(world, E.INTERVIEW_FEEDBACK_REQUESTED, schedule, person))
    assert "ROUND_NOT_ENDED" in _codes(found)


def test_a_feedback_request_after_the_round_passes_once(world):
    schedule, (person,) = _round(world, start=NOW - timedelta(hours=3))
    first = _interviewer_email(world, E.INTERVIEW_FEEDBACK_REQUESTED, schedule, person)
    assert send_policy.problems(world.db, first) == []
    _saved(world, first)
    again = _interviewer_email(world, E.INTERVIEW_FEEDBACK_REQUESTED, schedule, person)
    assert "RECENTLY_REQUESTED" in _codes(send_policy.problems(world.db, again))


def test_no_feedback_request_when_given_or_not_on_the_round(world):
    schedule, (person,) = _round(world, start=NOW - timedelta(hours=3))
    world.db.add(InterviewFeedback(interview_schedule_id=schedule.id, interviewer_id=person.id,
                                   recommendation=InterviewFeedbackRecommendation.ADVANCE))
    person.is_active = False
    world.db.flush()
    found = _codes(send_policy.problems(world.db, _interviewer_email(world, E.INTERVIEW_FEEDBACK_REQUESTED, schedule, person)))
    assert {"FEEDBACK_GIVEN", "INTERVIEWER_NOT_ON_ROUND"} <= found


# --- the send paths enforce them -----------------------------------------------------------------

def _rows(world, event):
    return world.db.execute(select(EmailNotification).where(EmailNotification.campaign_candidate_id == world.cc.id,
                                                            EmailNotification.trigger_event == event)).scalars().all()


def test_queueing_skips_a_refused_email(world):
    from app.services.notifications.candidate_notification_emails import (
        queue_candidate_selected_email, queue_interview_scheduled_email,
    )
    schedule, people = _round(world)               # the candidate is REJECTED, not in the Interview stage
    with patch("app.tasks.email_tasks.send_candidate_email_task.apply_async") as dispatch:
        queue_candidate_selected_email(world.db, world.cc)
        queue_interview_scheduled_email(world.db, world.cc, schedule, people)
    assert _rows(world, E.CANDIDATE_SELECTED) == [] and _rows(world, E.INTERVIEW_SCHEDULED) == []
    dispatch.assert_not_called()


def _service(world):
    from app.repositories.CampaignRepository import CampaignRepository
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    return CampaignCandidateService(campaign_repo=CampaignRepository(world.db),
                                    campaign_candidate_repo=CampaignCandidateRepository(world.db),
                                    audit_service=MagicMock())


def test_the_manual_rejection_prepares_one_draft_then_is_refused_once_sent(world):
    service = _service(world)
    with patch("app.tasks.email_tasks.send_candidate_email_task.apply_async") as dispatch:
        first = service.send_rejection_email(world.cc.id, actor_id="hr", actor_roles=["HR_ADMIN"])
        again = service.send_rejection_email(world.cc.id, actor_id="hr", actor_roles=["HR_ADMIN"])
        assert first.status == "draft" and again.draft_id == first.draft_id      # preparing again reopens it
        draft = world.db.get(EmailNotification, first.draft_id)
        draft.status = EmailNotificationStatus.SENT                              # as if previewed and sent
        world.db.flush()
        with pytest.raises(CampaignException) as refused:
            service.send_rejection_email(world.cc.id, actor_id="hr", actor_roles=["HR_ADMIN"])
    assert refused.value.status_code == 409 and refused.value.data["problems"][0]["code"] in ("ALREADY_SENT", "DUPLICATE")
    assert len(_rows(world, E.CANDIDATE_REJECTED)) == 1 and not dispatch.called


def test_the_manual_selection_is_refused_after_a_rejection_email(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED))
    world.cc.pipeline_stage = PipelineStage.SELECTED
    world.db.flush()
    with pytest.raises(CampaignException) as refused:
        _service(world).send_selection_email(world.cc.id, actor_id="hr", actor_roles=["HR_ADMIN"])
    assert refused.value.status_code == 409
    assert "OTHER_OUTCOME_SENT" in {p["code"] for p in refused.value.data["problems"]}
    assert _rows(world, E.CANDIDATE_SELECTED) == []


def test_the_bulk_check_lists_who_will_and_wont_receive_it(world):
    result = _service(world).check_bulk_rejection_email([world.cc.id, world.cc.id, uuid4()], actor_id="hr",
                                                        actor_roles=["HR_ADMIN"])
    assert [e["campaign_candidate_id"] for e in result.will_receive] == [str(world.cc.id)]
    assert result.wont_receive[0]["problems"][0]["code"] == "NOT_FOUND" and result.repeated == 1
    assert _rows(world, E.CANDIDATE_REJECTED) == []           # a check queues nothing


def test_the_send_task_blocks_an_email_whose_candidate_asked_for_erasure(world):
    from app.tasks.email_tasks import send_candidate_email_task
    queued = _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=EmailNotificationStatus.QUEUED)
    world.candidate.erasure_requested_at = NOW
    world.db.flush()
    with patch("app.tasks.email_tasks.SessionLocal", return_value=world.db), \
            patch.object(world.db, "close"), \
            patch("app.tasks.email_tasks.SESEmailClient", side_effect=AssertionError("the sender was called")):
        send_candidate_email_task.apply(kwargs={"email_notification_id": str(queued.id)})
    world.db.refresh(queued)
    assert queued.status == EmailNotificationStatus.BLOCKED and queued.block_code == "ERASURE_REQUESTED"
