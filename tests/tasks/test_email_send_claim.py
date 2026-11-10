"""
Review item 7: the send task claims its row (QUEUED -> SENDING) before calling SES, and never sends a
claimed row again on its own - a redelivered task, a concurrent run, or a send whose outcome is unknown
never reaches SES a second time.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.safe_errors import SafeRetryError
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from app.exceptions.email_exception import EmailDeliveryException
from app.models.email import EmailNotificationStatus
from tests.tasks.test_email_tasks import _Harness, _make_candidate, _make_notification, _make_template


def _ready(h, status=EmailNotificationStatus.QUEUED):
    notification = _make_notification(status=status)
    h.notification_repo.get_by_id.return_value = notification
    h.notification_repo.claim_for_sending.return_value = True
    h.template_repo.get_by_id.return_value = _make_template()
    h.candidate_repo.get_by_id.return_value = _make_candidate()
    h.campaign_candidate_repo.get_by_id.return_value = None
    return notification


def _send(notification):
    from app.tasks.email_tasks import send_candidate_email_task
    return send_candidate_email_task(email_notification_id=str(notification.id))


def test_the_row_is_claimed_before_ses_is_called():
    with _Harness() as h:
        notification = _ready(h)
        order = []
        h.notification_repo.claim_for_sending.side_effect = lambda _id: order.append("claim") or True
        h.ses_client_instance.send_email.side_effect = lambda **_: order.append("send") or "ses-id"
        _send(notification)
        assert order == ["claim", "send"]
        h.notification_repo.claim_for_sending.assert_called_once_with(notification.id)
        assert notification.status == EmailNotificationStatus.SENT


def test_a_run_that_loses_the_claim_never_calls_ses():
    with _Harness() as h:
        notification = _ready(h)
        h.notification_repo.claim_for_sending.return_value = False
        _send(notification)
        h.ses_client_instance.send_email.assert_not_called()
        h.dead_letter_queue_repo.create.assert_not_called()


@pytest.mark.parametrize("status", [EmailNotificationStatus.SENDING, EmailNotificationStatus.FAILED])
def test_a_claimed_or_failed_row_is_never_sent_by_a_redelivered_task(status):
    with _Harness() as h:
        notification = _ready(h, status=status)
        _send(notification)
        h.notification_repo.claim_for_sending.assert_not_called()
        h.ses_client_instance.send_email.assert_not_called()
        assert notification.status == status


@pytest.mark.parametrize("original", [
    ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "SendEmail"),
    EndpointConnectionError(endpoint_url="https://email.example.invalid"),
])
def test_a_send_ses_certainly_refused_gives_the_claim_back_and_retries(original):
    with _Harness() as h:
        notification = _ready(h)
        h.ses_client_instance.send_email.side_effect = EmailDeliveryException("SES send_email failed", original)
        with pytest.raises(SafeRetryError):   # review item 28: no SES text
            _send(notification)
        h.notification_repo.release_claim.assert_called_once_with(notification.id)
        h.dead_letter_queue_repo.create.assert_not_called()


def test_a_send_with_an_unknown_outcome_is_never_retried_and_stays_claimed():
    with _Harness() as h:
        notification = _ready(h)

        def claim(_id):         # what the claim leaves in the database
            notification.status = EmailNotificationStatus.SENDING
            return True
        h.notification_repo.claim_for_sending.side_effect = claim
        timeout = ReadTimeoutError(endpoint_url="https://email.example.invalid")
        h.ses_client_instance.send_email.side_effect = EmailDeliveryException("SES send_email failed", timeout)
        _send(notification)                                          # no retry raised
        h.notification_repo.release_claim.assert_not_called()
        h.dead_letter_queue_repo.create.assert_called_once()
        assert "outcome unknown" in h.dead_letter_queue_repo.create.call_args.kwargs["final_error_message"]
        assert notification.status == EmailNotificationStatus.SENDING   # FAILED would free its dedup key
        assert "outcome unknown" in notification.error_reason


def test_a_refused_client_counts_as_not_sent():
    """SESEmailClient's refusal (log mode / no sender) arrives wrapped twice; it never sent anything."""
    from app.tasks.email_tasks import _not_sent
    refusal = ValueError("SES client refused")
    wrapped = EmailDeliveryException("SES send_email failed", EmailDeliveryException(str(refusal), refusal))
    assert _not_sent(wrapped)
    assert not _not_sent(EmailDeliveryException("x", ReadTimeoutError(endpoint_url="u")))
    assert not _not_sent(EmailDeliveryException("x", ConnectionResetError()))


def test_a_send_whose_recording_fails_is_not_retried_or_dead_lettered():
    with _Harness() as h:
        notification = _ready(h)
        h.ses_client_instance.send_email.return_value = "ses-id"
        h.notification_repo.commit.side_effect = RuntimeError("db went away")
        _send(notification)                                          # no retry raised
        h.ses_client_instance.send_email.assert_called_once()
        h.dead_letter_queue_repo.create.assert_not_called()
        h.notification_repo.release_claim.assert_not_called()


def test_a_failure_before_the_claim_retries_without_touching_the_claim():
    with _Harness() as h:
        notification = _ready(h)
        h.template_repo.get_by_id.side_effect = ConnectionError("db blip")
        with pytest.raises(SafeRetryError):
            _send(notification)
        h.notification_repo.claim_for_sending.assert_not_called()
        h.notification_repo.release_claim.assert_not_called()
        h.ses_client_instance.send_email.assert_not_called()
