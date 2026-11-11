"""Against Postgres (rolled back): email templates edited on the Settings page - versioned, validated, no send."""
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.email import EmailTemplate, EmailTriggerEvent
from app.services.settings.email_template_settings import EmailTemplateSettingsService
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture

EVENT = "CANDIDATE_REJECTED"


def _service(db):  # noqa: F811
    audit = MagicMock()
    return EmailTemplateSettingsService(db, audit), audit


def _active(db, event=EVENT):  # noqa: F811
    return db.query(EmailTemplate).filter(EmailTemplate.trigger_event == EmailTriggerEvent(event),
                                          EmailTemplate.is_active.is_(True)).one()


def test_every_email_is_listed_and_the_unsent_one_is_not_editable(db):  # noqa: F811
    service, _ = _service(db)
    rows = {r["trigger_event"]: r for r in service.list()}
    assert set(rows) == {e.value for e in EmailTriggerEvent}
    assert rows["UPLOAD_PERMANENTLY_FAILED"]["editable"] is False
    assert rows[EVENT]["active"]["version"] >= 1


def test_a_save_is_a_new_version_and_the_old_one_stays_for_queued_emails(db):  # noqa: F811
    service, audit = _service(db)
    old = _active(db)
    result = service.save(EVENT, {"name": old.name, "subject": "Update on {job_title}",
                                  "body": "<p>Dear {candidate_name}, thank you.</p>"}, actor_id=None)
    new = _active(db)
    assert result["changed"] and new.id != old.id and new.version == old.version + 1
    db.refresh(old)
    assert old.is_active is False                                      # kept, inactive
    details = audit.log.call_args.kwargs["details"]
    assert details == {"title": "Email template updated", "trigger_event": EVENT, "version": new.version,
                       "from_template_id": str(old.id), "to_template_id": str(new.id)}


def test_an_unchanged_save_writes_nothing(db):  # noqa: F811
    service, audit = _service(db)
    old = _active(db)
    result = service.save(EVENT, {"name": old.name, "subject": old.subject, "body": old.body_template},
                          actor_id=None)
    assert result["changed"] is False and _active(db).id == old.id and not audit.log.called


@pytest.mark.parametrize("subject, body, field", [
    ("Hi {candidate_name.__class__}", "<p>x</p>", "subject"),
    ("Update", "<p>{interview_date}</p>", "body"),                  # not supplied for a rejection
    ("Update", "<p>{candidate_name</p>", "body"),
    ("", "<p>x</p>", "subject"),
])
def test_invalid_templates_are_refused_before_anything_is_saved(db, subject, body, field):  # noqa: F811
    service, audit = _service(db)
    old = _active(db)
    with pytest.raises(CampaignException) as exc:
        service.save(EVENT, {"name": "x", "subject": subject, "body": body}, actor_id=None)
    assert exc.value.status_code == 422 and field in exc.value.data["problems"]
    assert _active(db).id == old.id and not audit.log.called


def test_the_unsent_email_cant_be_edited(db):  # noqa: F811
    service, _ = _service(db)
    with pytest.raises(CampaignException):
        service.save("UPLOAD_PERMANENTLY_FAILED", {"name": "x", "subject": "s", "body": "b"}, actor_id=None)


def test_preview_renders_sample_values_escaped_and_sends_nothing(db):  # noqa: F811
    service, audit = _service(db)
    with patch("app.tasks.email_tasks.send_candidate_email_task") as task:
        result = service.preview("INTERVIEW_INTERVIEWER_INVITATION", {
            "subject": "Interview with {candidate_name}", "body": "<p>{notes_block}</p>"})
    assert result["subject"] == "Interview with Alex Sample"
    assert "<br>" in result["html"] and "Please focus" in result["html"]
    task.apply_async.assert_not_called()
    assert not audit.log.called


def test_restore_brings_an_old_version_back_as_a_new_one(db):  # noqa: F811
    service, audit = _service(db)
    original = _active(db)
    service.save(EVENT, {"name": original.name, "subject": "Changed", "body": "<p>{candidate_name}</p>"},
                 actor_id=None)
    service.restore(EVENT, str(original.id), actor_id=None)
    current = _active(db)
    assert (current.subject, current.body_template) == (original.subject, original.body_template)
    assert current.id != original.id and current.version == original.version + 2
    assert audit.log.call_args.kwargs["details"]["restored_from_version"] == original.version


def test_restoring_the_version_in_use_or_another_events_version_is_refused(db):  # noqa: F811
    service, _ = _service(db)
    with pytest.raises(CampaignException):
        service.restore(EVENT, str(_active(db).id), actor_id=None)
    with pytest.raises(CampaignException) as exc:
        service.restore(EVENT, str(_active(db, "CANDIDATE_SELECTED").id), actor_id=None)
    assert exc.value.status_code == 404
