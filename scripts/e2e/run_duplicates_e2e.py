"""
Never twice (email plan phase 5) - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_duplicates_e2e

Every way an email could go out twice, against the real API, the real send task and the stack's own worker,
over SMTP to the stack's Mailpit (which relays nothing). Each case has its own synthetic candidate or
interviewer (<name>@e2e.invalid) and expects exactly one Mailpit message to that address - counted after
the message arrives and again after a settle time, so a late duplicate is caught.

  D1  double-clicked Send: two Sends of the same previewed draft at once - one is accepted, one refused (409)
  D2  broker redelivery: the confirmed email's send task delivered again to the worker and run three more times
      in this process at once - one claim, one message
  D3  retry after a failure before acceptance: the first SMTP attempt is refused (not sent), the claim is given
      back and the retry sends it - one message
  D4  retry after acceptance, outcome unknown: Mailpit takes the message, then the connection drops - the row
      stays SENDING (never retried) and redeliveries send nothing more
  D5  retry after acceptance, recording failed: Mailpit takes the message, then recording it fails - the row
      stays SENDING and redeliveries send nothing more
  D6  overlapping sweeps: the hourly feedback-request sweep run five times at once (three through its
      single-run lock, two straight into the sweep) - one draft per interviewer and nothing sent; that draft,
      sent, is one message
  D7  overlapping stuck-send alerts: D4/D5's rows, made stuck, and the stuck-send check run three times at once
      and once after - one alert per HR admin
  D8  bulk send with a repeated candidate, double-clicked: two bulk sends of [C, C, D] at once - one message
      each to C and D
  D9  no draft is sent without a confirm: a prepared rejection draft and an unconfirmed feedback draft, with
      their send task dispatched to the worker and run here - both stay DRAFT, nothing to either address

Report: /data/storage/e2e_duplicates_report.json
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from scripts.e2e import mailpit
from scripts.e2e.run_fallback_e2e import wait_for
from scripts.e2e.run_reparse_e2e import EmailAttempted, Report
from scripts.e2e.setup_e2e import assert_isolated

REPORT = Path("/data/storage/e2e_duplicates_report.json")
HR = {"Authorization": "Bearer e2e:e2e-hr-admin"}
SETTLE_SECONDS = 10          # after the expected message arrives: time for a (wrong) second one to arrive too


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
        Run().all()


def at_once(*calls):
    """Runs the calls in threads released together; returns their results (or exceptions) in order."""
    results = [None] * len(calls)
    gate = threading.Barrier(len(calls))

    def go(i, fn):
        gate.wait()
        try:
            results[i] = fn()
        except Exception as exc:          # noqa: BLE001 - reported, not raised
            results[i] = exc
    threads = [threading.Thread(target=go, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    return results


def to(address: str) -> int:
    return mailpit.count(f'to:"{address}"')


class Run:
    def __init__(self):
        import app.models  # noqa: F401
        import app.main  # noqa: F401 - registers every model
        from fastapi.testclient import TestClient
        from app.db.session import SessionLocal
        from app.enums.constants import API_PREFIX
        self.api_base = f"http://e2e.local{API_PREFIX}"
        self.client = lambda: TestClient(app.main.app, base_url=self.api_base, client=("127.0.0.1", 50000))
        self.http = self.client()
        self.db = SessionLocal()
        self.report = Report()
        self.steps: dict = {}

    # ---- helpers ------------------------------------------------------------------------------------------
    def candidate(self, label: str):
        """A synthetic REJECTED candidate in the first campaign, with their own address <label>@e2e.invalid."""
        from sqlalchemy import inspect, select
        from app.core.encryption_service import EncryptionService
        from app.models.candidates import Candidate, Resume
        from app.models.pipeline import CampaignCandidate, PipelineStage
        from app.repositories.encryption_key_repository import EncryptionKeyRepository
        db = self.db
        source = db.execute(select(CampaignCandidate).order_by(CampaignCandidate.created_at)).scalars().first()
        address = f"dup-{label.lower()}-{uuid4().hex[:6]}@e2e.invalid"
        crypto = EncryptionService(EncryptionKeyRepository(db))
        name_ct, key_id = crypto.encrypt(f"E2E Duplicate {label}", "CANDIDATE_PII")
        email_ct, _ = crypto.encrypt(address, "CANDIDATE_PII")
        person = Candidate(id=uuid4(), full_name_encrypted=name_ct, email_encrypted=email_ct,
                           email_hash=crypto.generate_hash(address), encryption_key_id=key_id,
                           consent_given=True, consent_timestamp=datetime.now(timezone.utc), consent_source="e2e")
        db.add(person)
        db.flush()
        resume_src = db.get(Resume, source.resume_id)
        values = {a.key: getattr(resume_src, a.key) for a in inspect(Resume).column_attrs
                  if a.key not in ("id", "created_at", "updated_at")}
        values.update(candidate_id=person.id, file_path=f"e2e/duplicates/{uuid4().hex}.docx",
                      file_hash=uuid4().hex, bulk_upload_job_id=None)
        resume = Resume(id=uuid4(), **values)
        db.add(resume)
        db.flush()
        cc = CampaignCandidate(id=uuid4(), campaign_id=source.campaign_id, candidate_id=person.id,
                               resume_id=resume.id, idempotency_key=f"e2e-dup-{uuid4().hex}",
                               pipeline_stage=PipelineStage.REJECTED, is_fraud_flagged=False)
        db.add(cc)
        db.commit()
        return cc, address

    def prepared(self, cc):
        """Prepare the rejection email (a DRAFT) and preview it: (draft_id, preview_hash)."""
        prepared = self.http.post(f"/campaign-candidates/{cc.id}/send-rejection-email", headers=HR)
        draft_id = (prepared.json().get("data") or {}).get("draft_id")
        preview = self.http.post(f"/email-drafts/{draft_id}/preview", headers=HR)
        return draft_id, (preview.json().get("data") or {}).get("preview_hash")

    def row(self, notification_id):
        from app.models.email import EmailNotification
        self.db.expire_all()
        return self.db.get(EmailNotification, notification_id)

    def arrived(self, address: str, expected: int = 1, timeout: int = 120) -> int:
        """Waits for the expected count at that address, then the settle time; returns the final count."""
        try:
            wait_for(lambda: to(address) >= expected or None, f"{expected} message(s) to {address}", timeout=timeout)
        except TimeoutError:
            pass
        time.sleep(SETTLE_SECONDS)
        return to(address)

    @staticmethod
    def run_task_here(notification_id):
        from app.tasks.email_tasks import send_candidate_email_task
        return send_candidate_email_task.apply(kwargs={"email_notification_id": str(notification_id)})

    @staticmethod
    def dispatch(notification_id):
        from app.tasks.email_tasks import send_candidate_email_task
        return send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification_id)})

    def send_here(self, draft_id, preview_hash, smtp=None, lock=None):
        """Send the draft through the API, with its send task run in this process (fault injection)."""
        from app.services.notifications.ses_email_client import SESEmailClient
        from app.tasks.email_tasks import send_candidate_email_task
        patches = [patch.object(send_candidate_email_task, "apply_async",
                                lambda kwargs=None, **_: send_candidate_email_task.apply(kwargs=kwargs))]
        if smtp:
            patches.append(patch.object(SESEmailClient, "_send_smtp", smtp))
        if lock:
            patches.append(patch("app.services.campaign.stage_lock.lock_after_send", lock))
        for p in patches:
            p.start()
        try:
            return self.http.post(f"/email-drafts/{draft_id}/send", headers=HR, json={"preview_hash": preview_hash})
        finally:
            for p in reversed(patches):
                p.stop()

    # ---- the cases ----------------------------------------------------------------------------------------
    def all(self) -> None:
        mailpit.clear()
        stuck_rows = []
        for case in (self.d1, self.d2, self.d3):
            case()
        stuck_rows.append(self.d4())
        stuck_rows.append(self.d5())
        self.d6()
        self.d7([r for r in stuck_rows if r is not None])
        self.d8()
        self.d9()
        mailpit.clear()
        REPORT.write_text(json.dumps({"checks": self.report.rows, "steps": self.steps}, indent=1, default=str),
                          "utf-8")
        print(f"{sum(r['result'] == 'PASS' for r in self.report.rows)}/{len(self.report.rows)} duplicate checks "
              f"passed. Report: {REPORT}")
        self.db.close()
        if any(r["result"] == "FAIL" for r in self.report.rows):
            raise SystemExit(1)

    def d1(self):
        cc, address = self.candidate("A")
        draft_id, preview_hash = self.prepared(cc)
        clients = [self.client(), self.client()]
        sends = at_once(*[lambda c=c: c.post(f"/email-drafts/{draft_id}/send", headers=HR,
                                             json={"preview_hash": preview_hash}) for c in clients])
        codes = sorted(getattr(s, "status_code", repr(s)) for s in sends)
        count = self.arrived(address)
        row = self.row(draft_id)
        self.steps["D1"] = {"sends": codes, "mailpit": count, "status": row and row.status.value}
        self.report.check(1, "double-clicked Send: one accepted, one refused (409); exactly one message",
                          codes == [200, 409] and count == 1 and row.status.value == "SENT",
                          json.dumps(self.steps["D1"]))

    def d2(self):
        cc, address = self.candidate("B")
        draft_id, preview_hash = self.prepared(cc)
        sent = self.http.post(f"/email-drafts/{draft_id}/send", headers=HR, json={"preview_hash": preview_hash})
        # The broker delivers it again (twice), and three more runs start here at the same moment.
        self.dispatch(draft_id)
        self.dispatch(draft_id)
        at_once(*[lambda: self.run_task_here(draft_id) for _ in range(3)])
        count = self.arrived(address)
        time.sleep(5)                            # the redelivered tasks reach the worker after the first
        count = to(address)
        row = self.row(draft_id)
        self.steps["D2"] = {"send": sent.status_code, "mailpit": count, "status": row and row.status.value}
        self.report.check(2, "broker redelivery and concurrent runs of the same send: exactly one message",
                          sent.status_code == 200 and count == 1 and row.status.value == "SENT",
                          json.dumps(self.steps["D2"]))

    def d3(self):
        from app.exceptions.email_exception import EmailDeliveryException
        from app.services.notifications.ses_email_client import SESEmailClient, SMTPNotSent
        cc, address = self.candidate("C3")
        draft_id, preview_hash = self.prepared(cc)
        real, calls = SESEmailClient._send_smtp, []

        def refused_once(client, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise EmailDeliveryException("e2e: refused before accepting", SMTPNotSent("e2e"))
            return real(client, **kwargs)
        sent = self.send_here(draft_id, preview_hash, smtp=refused_once)
        after_first = self.row(draft_id).status.value
        retried_here = len(calls) > 1
        if not retried_here:                      # the retry was scheduled, not run eagerly: the worker runs it
            self.dispatch(draft_id)
        count = self.arrived(address)
        row = self.row(draft_id)
        self.steps["D3"] = {"send": sent.status_code, "smtp_attempts_here": len(calls), "retried_here": retried_here,
                            "status_after_first": after_first, "mailpit": count, "status": row.status.value}
        self.report.check(3, "a send refused before acceptance is retried and sent once",
                          sent.status_code == 200 and count == 1 and row.status.value == "SENT",
                          json.dumps(self.steps["D3"]))

    def _redeliver(self, notification_id):
        self.dispatch(notification_id)
        self.dispatch(notification_id)
        self.run_task_here(notification_id)
        time.sleep(SETTLE_SECONDS)

    def d4(self):
        import smtplib
        from app.exceptions.email_exception import EmailDeliveryException
        from app.services.notifications.ses_email_client import SESEmailClient
        cc, address = self.candidate("D4")
        draft_id, preview_hash = self.prepared(cc)
        real = SESEmailClient._send_smtp

        def accepted_then_dropped(client, **kwargs):
            real(client, **kwargs)
            raise EmailDeliveryException("e2e: connection dropped after the data",
                                         smtplib.SMTPServerDisconnected("e2e"))
        sent = self.send_here(draft_id, preview_hash, smtp=accepted_then_dropped)
        first = self.arrived(address)
        row = self.row(draft_id)
        status_after = row.status.value
        self._redeliver(draft_id)
        count = to(address)
        self.steps["D4"] = {"send": sent.status_code, "mailpit_after_send": first, "status": status_after,
                            "outcome_unknown": "outcome unknown" in (row.error_reason or ""),
                            "mailpit_after_redelivery": count}
        self.report.check(4, "accepted then dropped: stays SENDING, never retried; exactly one message",
                          sent.status_code == 200 and first == 1 and status_after == "SENDING"
                          and self.steps["D4"]["outcome_unknown"] and count == 1,
                          json.dumps(self.steps["D4"]))
        return draft_id

    def d5(self):
        cc, address = self.candidate("D5")
        draft_id, preview_hash = self.prepared(cc)

        def recording_fails(*_args, **_kwargs):
            raise RuntimeError("e2e: recording the send failed")
        sent = self.send_here(draft_id, preview_hash, lock=recording_fails)
        first = self.arrived(address)
        status_after = self.row(draft_id).status.value
        self._redeliver(draft_id)
        count = to(address)
        self.steps["D5"] = {"send": sent.status_code, "mailpit_after_send": first, "status": status_after,
                            "mailpit_after_redelivery": count}
        self.report.check(5, "accepted, recording failed: stays SENDING; redeliveries send nothing more",
                          sent.status_code == 200 and first == 1 and status_after == "SENDING" and count == 1,
                          json.dumps(self.steps["D5"]))
        return draft_id

    def d6(self):
        from app.models.email import EmailNotification, EmailTriggerEvent
        from app.models.interview import InterviewInterviewer, InterviewPlatform, InterviewSchedule, InterviewStatus
        from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
        from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
        from app.repositories.interview_schedule_repository import InterviewScheduleRepository
        from app.services.interview_feedback_request_sweep_service import InterviewFeedbackRequestSweepService
        from app.db.session import SessionLocal
        from app.models.pipeline import PipelineStage
        from app.tasks.interview_tasks import request_interview_feedback_task
        cc, _ = self.candidate("Round")
        cc.pipeline_stage = PipelineStage.INTERVIEW
        ended = datetime.now(timezone.utc) - timedelta(hours=2)
        schedule = InterviewSchedule(id=uuid4(), campaign_candidate_id=cc.id, round_number=1,
                                     status=InterviewStatus.SCHEDULED, start_at=ended - timedelta(hours=1),
                                     end_at=ended, timezone="UTC", platform=InterviewPlatform.PHONE)
        self.db.add(schedule)
        self.db.flush()
        people = []
        for label in ("one", "two"):
            address = f"dup-interviewer-{label}-{uuid4().hex[:6]}@e2e.invalid"
            person = InterviewInterviewer(id=uuid4(), interview_id=schedule.id, name=f"E2E Interviewer {label}",
                                          email=address, is_active=True)
            self.db.add(person)
            people.append((person, address))
        self.db.commit()

        def sweep_straight():
            db = SessionLocal()
            try:
                return InterviewFeedbackRequestSweepService(db, InterviewScheduleRepository(db),
                                                            InterviewFeedbackRepository(db),
                                                            CampaignCandidateRepository(db)).run()
            finally:
                db.close()
        at_once(*([lambda: request_interview_feedback_task.apply()] * 3 + [sweep_straight] * 2))
        time.sleep(SETTLE_SECONDS)
        self.db.expire_all()
        drafts = (self.db.query(EmailNotification)
                  .filter(EmailNotification.interview_schedule_id == schedule.id,
                          EmailNotification.trigger_event == EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED).all())
        per_person = {str(p.id): sum(1 for d in drafts if d.interview_interviewer_id == p.id) for p, _ in people}
        before_send = sum(to(a) for _, a in people)
        first = next(d for d in drafts if d.interview_interviewer_id == people[0][0].id) if drafts else None
        sends, count = [], None
        if first is not None:
            preview = self.http.post(f"/email-drafts/{first.id}/preview", headers=HR)
            preview_hash = (preview.json().get("data") or {}).get("preview_hash")
            clients = [self.client(), self.client()]
            sends = sorted(getattr(s, "status_code", repr(s)) for s in at_once(
                *[lambda c=c: c.post(f"/email-drafts/{first.id}/send", headers=HR,
                                     json={"preview_hash": preview_hash}) for c in clients]))
            count = self.arrived(people[0][1])
        self.steps["D6"] = {"drafts": len(drafts), "per_interviewer": list(per_person.values()),
                            "statuses": sorted(d.status.value for d in drafts), "mailpit_before_send": before_send,
                            "sends": sends, "mailpit_after_send": count}
        self.report.check(6, "five overlapping feedback sweeps: one draft per interviewer, nothing sent; "
                             "that draft sent once",
                          sorted(per_person.values()) == [1, 1] and before_send == 0 and sends == [200, 409]
                          and count == 1,
                          json.dumps(self.steps["D6"]))
        self.unconfirmed_feedback = (next(d.id for d in drafts if d.interview_interviewer_id == people[1][0].id),
                                     people[1][1]) if len(drafts) == 2 else None

    def d7(self, stuck_ids):
        from sqlalchemy import update
        from app.models.email import EmailNotification, EmailTriggerEvent
        from app.tasks.email_health_tasks import monitor_stuck_sends
        A = EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT
        self.db.execute(update(EmailNotification).where(EmailNotification.id.in_(stuck_ids))
                        .values(sending_since=datetime.now(timezone.utc) - timedelta(hours=1)))
        self.db.commit()
        already = self.db.query(EmailNotification).filter(EmailNotification.trigger_event == A).count()
        at_once(*[lambda: monitor_stuck_sends.apply()] * 3)
        monitor_stuck_sends.apply()
        self.db.expire_all()
        alerts = (self.db.query(EmailNotification).filter(EmailNotification.trigger_event == A)
                  .order_by(EmailNotification.created_at).all())[already:]
        addresses = [a.recipient_email for a in alerts]
        counts = {}
        for address in set(addresses):
            counts[address] = self.arrived(address)
        self.steps["D7"] = {"stuck_rows": len(stuck_ids), "alerts_queued": len(alerts),
                            "distinct_recipients": len(set(addresses)), "mailpit_per_recipient": sorted(counts.values())}
        self.report.check(7, "overlapping stuck-send checks: one alert per HR admin, one message each",
                          len(stuck_ids) == 2 and len(alerts) >= 1 and len(alerts) == len(set(addresses))
                          and all(c == 1 for c in counts.values()),
                          json.dumps(self.steps["D7"]))

    def d8(self):
        (c, c_address), (d, d_address) = self.candidate("C8"), self.candidate("D8")
        ids = [str(c.id), str(c.id), str(d.id)]
        check = self.http.post("/campaign-candidates/bulk-rejection-email/check", headers=HR,
                               json={"campaign_candidate_ids": ids})
        cdata = check.json().get("data") or {}
        clients = [self.client(), self.client()]
        sends = at_once(*[lambda cl=cl: cl.post("/campaign-candidates/bulk-send-rejection-email", headers=HR,
                                                json={"campaign_candidate_ids": ids,
                                                      "check_hash": cdata.get("check_hash")}) for cl in clients])
        queued = sorted(len(((s.json().get("data") or {}).get("queued") or [])) if hasattr(s, "json") else -1
                        for s in sends)
        counts = [self.arrived(c_address), self.arrived(d_address)]
        self.steps["D8"] = {"check": check.status_code, "will": len(cdata.get("will_receive") or []),
                            "repeated": cdata.get("repeated"), "sends": [getattr(s, "status_code", repr(s)) for s in sends],
                            "queued_per_send": queued, "mailpit": counts}
        self.report.check(8, "two bulk sends of [C, C, D] at once: one message each to C and D",
                          check.status_code == 200 and self.steps["D8"]["will"] == 2 and cdata.get("repeated") == 1
                          and sum(q for q in queued if q > 0) == 2 and counts == [1, 1],
                          json.dumps(self.steps["D8"]))

    def d9(self):
        cc, address = self.candidate("E")
        draft_id, _ = self.prepared(cc)
        targets = [(draft_id, address)] + ([self.unconfirmed_feedback] if getattr(self, "unconfirmed_feedback", None)
                                            else [])
        for notification_id, _ in targets:
            self.dispatch(notification_id)
            self.run_task_here(notification_id)
        time.sleep(SETTLE_SECONDS)
        statuses = [self.row(n).status.value for n, _ in targets]
        counts = [to(a) for _, a in targets]
        self.steps["D9"] = {"drafts": len(targets), "statuses": statuses, "mailpit": counts}
        self.report.check(9, "unconfirmed drafts, with their send task run anyway: still DRAFT, nothing sent",
                          len(targets) == 2 and statuses == ["DRAFT", "DRAFT"] and counts == [0, 0],
                          json.dumps(self.steps["D9"]))


if __name__ == "__main__":
    main()
