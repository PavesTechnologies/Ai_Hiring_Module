"""
EMAIL_DELIVERY=smtp end to end - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_mailpit_e2e

One internal HR alert (the embedding health alert) to the synthetic e2e HR admin, queued like the real job
queues it and sent by the stack's own worker over SMTP to the stack's Mailpit - which relays nothing.

  M1  the worker sends exactly one email, and Mailpit holds exactly one message, to hr.admin@e2e.invalid,
      with the rendered subject
  M2  the notification is SENT (not LOGGED) with the SMTP Message-ID Mailpit holds
  M3  no SES client is created; Mailpit is cleared afterwards

Then the checks before every email (email plan phase 2) through the real API, as the e2e HR admin:

  M4  the bulk rejection check lists who will receive it (a REJECTED candidate, listed twice, counted once)
      and who won't (a candidate who isn't rejected: WRONG_STAGE); nothing is queued, Mailpit stays empty
  M5  preparing the rejection email for the REJECTED candidate makes a DRAFT and sends nothing (phase 3)
  M6  Send with a stale preview is refused (409 PREVIEW_CHANGED); with the previewed one the worker sends exactly
      one message, to the previewed recipient with the previewed subject, recorded as confirmed by the sender
  M7  a second Send of the same email and a new prepare are both refused (409); still exactly one message

Report: /data/storage/e2e_mailpit_report.json
"""
import json
from pathlib import Path
from unittest.mock import patch

from scripts.e2e import mailpit
from scripts.e2e.run_fallback_e2e import wait_for
from scripts.e2e.run_reparse_e2e import EmailAttempted, Report
from scripts.e2e.setup_e2e import USERS, assert_isolated

REPORT = Path("/data/storage/e2e_mailpit_report.json")
HR_ADMIN = "e2e-hr-admin"


def main() -> None:
    assert_isolated()
    import boto3
    real_client = boto3.client

    def guarded(service, *args, **kwargs):
        if service == "ses":
            raise EmailAttempted("an SES client was created")
        return real_client(service, *args, **kwargs)

    from scripts.e2e.run_reparse_e2e import _token_payload
    with patch("boto3.client", guarded), patch("app.middleware.jwt_middleware.decode_token", _token_payload):
        run()


def run() -> None:
    import app.models  # noqa: F401
    import app.main  # noqa: F401 - registers every model (email_notifications' FKs reach interview_schedules)
    from app.db.session import SessionLocal
    from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
    from app.models.identity import User
    from app.services.notifications.internal_user_emails import queue_internal_user_emails

    report = Report()
    steps: dict = {}
    address = USERS[HR_ADMIN][0]
    mailpit.clear()
    db = SessionLocal()
    hr_admin = db.get(User, HR_ADMIN)
    queued = queue_internal_user_emails(db, [hr_admin], EmailTriggerEvent.EMBEDDING_HEALTH_ALERT, {
        "campaign_name": "[E2E] Mailpit check", "affected_count": 3, "total_screening_count": 10,
        "failure_percentage": "30.00", "circuit_breaker_state": "CLOSED", "monitoring_link": "#",
        "suspended_note": ""})

    def finished():
        db.expire_all()
        row = (db.query(EmailNotification)
               .filter(EmailNotification.recipient_user_id == HR_ADMIN,
                       EmailNotification.trigger_event == EmailTriggerEvent.EMBEDDING_HEALTH_ALERT)
               .order_by(EmailNotification.created_at.desc()).first())
        return row if row is not None and row.status not in (EmailNotificationStatus.QUEUED,
                                                             EmailNotificationStatus.SENDING) else None

    try:
        row = wait_for(finished, "the worker to send the alert", timeout=120)
    except TimeoutError:
        row = None
    to_hr = wait_for(lambda: mailpit.messages(f'to:"{address}"') or None, "Mailpit to receive it", timeout=30) \
        if row is not None else []
    everything = mailpit.messages()
    message = to_hr[0] if to_hr else {}
    steps["M1"] = {"queued": queued, "mailpit_total": len(everything), "to_hr_admin": len(to_hr),
                   "subject": message.get("Subject"), "to": mailpit.recipients(message)}
    report.check(1, "the worker sends one email over SMTP; Mailpit holds exactly one, to the HR admin",
                 queued == 1 and len(everything) == 1 and len(to_hr) == 1
                 and "[E2E] Mailpit check" in (message.get("Subject") or ""),
                 json.dumps(steps["M1"]))

    message_id = (message.get("MessageID") or "").strip("<>")
    steps["M2"] = {"status": row and row.status.value, "provider_message_id": row and row.provider_message_id,
                   "mailpit_message_id": message_id}
    report.check(2, "the notification is SENT with the Message-ID Mailpit holds",
                 row is not None and row.status == EmailNotificationStatus.SENT
                 and (row.provider_message_id or "") == f"smtp-{message_id}" and bool(message_id),
                 json.dumps(steps["M2"]))

    mailpit.clear()
    report.check(3, "no SES client created in this process; Mailpit cleared", mailpit.count() == 0)
    _outcome_email_checks(db, report, steps)
    mailpit.clear()
    REPORT.write_text(json.dumps({"checks": report.rows, "steps": steps}, indent=1, default=str), "utf-8")
    print(f"{sum(r['result'] == 'PASS' for r in report.rows)}/{len(report.rows)} Mailpit checks passed. "
          f"Report: {REPORT}")
    db.close()
    if any(r["result"] == "FAIL" for r in report.rows):
        raise SystemExit(1)


def _outcome_email_checks(db, report, steps) -> None:
    import time
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.enums.constants import API_PREFIX
    from app.main import app as api
    from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
    from app.models.pipeline import CampaignCandidate, PipelineStage

    client = TestClient(api, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    hr = {"Authorization": "Bearer e2e:e2e-hr-admin"}
    db.expire_all()
    rejected = db.execute(select(CampaignCandidate).where(CampaignCandidate.pipeline_stage == PipelineStage.REJECTED)
                          .order_by(CampaignCandidate.created_at)).scalars().first()
    other = db.execute(select(CampaignCandidate).where(CampaignCandidate.pipeline_stage != PipelineStage.REJECTED)
                       .order_by(CampaignCandidate.created_at)).scalars().first()
    base = "/campaign-candidates"

    # M4 - the bulk check
    ids = [str(rejected.id), str(rejected.id), str(other.id)] if rejected and other else []
    check = client.post(f"{base}/bulk-rejection-email/check", headers=hr, json={"campaign_candidate_ids": ids})
    data = check.json().get("data") or {}
    wont_codes = [p["code"] for e in data.get("wont_receive", []) for p in e.get("problems", [])]
    steps["M4"] = {"http": check.status_code, "will": [e["campaign_candidate_id"] for e in data.get("will_receive", [])],
                   "wont_codes": wont_codes, "repeated": data.get("repeated"), "mailpit": mailpit.count()}
    report.check(4, "the bulk check lists who will and won't receive it, once each, and queues nothing",
                 check.status_code == 200 and steps["M4"]["will"] == [str(rejected.id)] and "WRONG_STAGE" in wont_codes
                 and data.get("repeated") == 1 and mailpit.count() == 0,
                 json.dumps(steps["M4"]))

    # M5 - preparing makes a draft and sends nothing (email plan phase 3)
    prepared = client.post(f"{base}/{rejected.id}/send-rejection-email", headers=hr) if rejected else None
    draft_id = ((prepared.json().get("data") or {}).get("draft_id")) if prepared is not None else None
    time.sleep(5)                                       # time for any (wrong) automatic send to arrive
    steps["M5"] = {"http": prepared.status_code if prepared is not None else None,
                   "status": (prepared.json().get("data") or {}).get("status") if prepared is not None else None,
                   "mailpit": mailpit.count()}
    report.check(5, "preparing the rejection email makes a draft and sends nothing",
                 prepared is not None and prepared.status_code == 200 and steps["M5"]["status"] == "draft"
                 and bool(draft_id) and mailpit.count() == 0,
                 json.dumps(steps["M5"]))

    # M6 - Send needs the previewed email; then the worker sends exactly one
    preview = client.post(f"/email-drafts/{draft_id}/preview", headers=hr) if draft_id else None
    pdata = (preview.json().get("data") or {}) if preview is not None else {}
    stale = client.post(f"/email-drafts/{draft_id}/send", headers=hr, json={"preview_hash": "0" * 64}) if draft_id else None
    sent = client.post(f"/email-drafts/{draft_id}/send", headers=hr,
                       json={"preview_hash": pdata.get("preview_hash")}) if draft_id else None

    def delivered():
        db.expire_all()
        row = db.execute(select(EmailNotification).where(
            EmailNotification.campaign_candidate_id == rejected.id,
            EmailNotification.trigger_event == EmailTriggerEvent.CANDIDATE_REJECTED)).scalars().first()
        return row if row is not None and row.status not in (EmailNotificationStatus.QUEUED,
                                                             EmailNotificationStatus.SENDING,
                                                             EmailNotificationStatus.DRAFT) else None

    try:
        row = wait_for(delivered, "the worker to send the rejection email", timeout=120)
        wait_for(lambda: mailpit.count() or None, "Mailpit to receive it", timeout=30)
    except TimeoutError:
        row = None
    message = (mailpit.messages() or [{}])[0]
    steps["M6"] = {"preview": preview.status_code if preview is not None else None, "can_send": pdata.get("can_send"),
                   "stale_send": stale.status_code if stale is not None else None,
                   "stale_code": ((stale.json().get("data") or {}).get("code")) if stale is not None else None,
                   "send": sent.status_code if sent is not None else None, "status": row and row.status.value,
                   "confirmed_by": row and row.confirmed_by, "mailpit": mailpit.count(),
                   "to_matches_preview": mailpit.recipients(message) == [pdata.get("recipient_address")],
                   "subject_matches_preview": message.get("Subject") == pdata.get("subject")}
    report.check(6, "Send needs the previewed email (409 otherwise); then exactly one message, as previewed",
                 preview is not None and preview.status_code == 200 and pdata.get("can_send") is True
                 and stale is not None and stale.status_code == 409 and steps["M6"]["stale_code"] == "PREVIEW_CHANGED"
                 and sent is not None and sent.status_code == 200 and row is not None
                 and row.status == EmailNotificationStatus.SENT and row.confirmed_by == "e2e-hr-admin"
                 and mailpit.count() == 1 and steps["M6"]["to_matches_preview"] and steps["M6"]["subject_matches_preview"],
                 json.dumps(steps["M6"]))

    # M7 - never twice: a second Send of the same email, and preparing a new one, are both refused
    again = client.post(f"/email-drafts/{draft_id}/send", headers=hr,
                        json={"preview_hash": pdata.get("preview_hash")}) if draft_id else None
    reprepare = client.post(f"{base}/{rejected.id}/send-rejection-email", headers=hr) if rejected else None
    time.sleep(5)                                       # time for any (wrong) second send to arrive
    codes = [p["code"] for p in ((reprepare.json().get("data") or {}).get("problems") or [])] if reprepare is not None else []
    steps["M7"] = {"send_again": again.status_code if again is not None else None,
                   "prepare_again": reprepare.status_code if reprepare is not None else None, "codes": codes,
                   "mailpit": mailpit.count()}
    report.check(7, "a second Send and a new prepare are refused (409); still exactly one message in Mailpit",
                 again is not None and again.status_code == 409 and reprepare is not None
                 and reprepare.status_code == 409 and bool({"ALREADY_SENT", "DUPLICATE"} & set(codes))
                 and mailpit.count() == 1,
                 json.dumps(steps["M7"]))


if __name__ == "__main__":
    main()
