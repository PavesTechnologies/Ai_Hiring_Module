"""
EMAIL_DELIVERY=smtp end to end - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_mailpit_e2e

One internal HR alert (the embedding health alert) to the synthetic e2e HR admin, queued like the real job
queues it and sent by the stack's own worker over SMTP to the stack's Mailpit - which relays nothing.

  M1  the worker sends exactly one email, and Mailpit holds exactly one message, to hr.admin@e2e.invalid,
      with the rendered subject
  M2  the notification is SENT (not LOGGED) with the SMTP Message-ID Mailpit holds
  M3  no SES client is created; Mailpit is cleared afterwards

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

    with patch("boto3.client", guarded):
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
    REPORT.write_text(json.dumps({"checks": report.rows, "steps": steps}, indent=1, default=str), "utf-8")
    print(f"{sum(r['result'] == 'PASS' for r in report.rows)}/{len(report.rows)} Mailpit checks passed. "
          f"Report: {REPORT}")
    db.close()
    if any(r["result"] == "FAIL" for r in report.rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
