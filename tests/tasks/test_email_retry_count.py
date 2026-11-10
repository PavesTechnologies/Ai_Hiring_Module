"""
Review item 30: the email send reads MAX_EMAIL_RETRY_COUNT (seeded 4) instead of a hardcoded 3.
"""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.exceptions.email_exception import EmailDeliveryException
from tests.tasks.test_email_tasks import _Harness, _make_candidate, _make_notification, _make_template

MODULE = "app.tasks.email_tasks"


@pytest.mark.parametrize("raw, attempts", [
    ("4", 4), ("6", 6), (None, 4),          # the seed, a change, no row
    ("0", 4), ("50", 4), ("lots", 4),       # out of range or not a number: the default
])
def test_the_policy_reads_the_setting(raw, attempts):
    from app.tasks.email_tasks import _email_retry_policy
    config = MagicMock()
    config.get_configs_by_keys.return_value = {} if raw is None else {"MAX_EMAIL_RETRY_COUNT": raw}
    with patch(f"{MODULE}.ConfigRepository", return_value=config):
        assert _email_retry_policy(MagicMock()).max_attempts == attempts


def test_an_unreadable_setting_falls_back_to_the_default():
    from app.tasks.email_tasks import _email_retry_policy
    with patch(f"{MODULE}.ConfigRepository", side_effect=RuntimeError("db down")):
        assert _email_retry_policy(MagicMock()).max_attempts == 4


@pytest.mark.parametrize("retries_so_far, retried", [(2, True), (3, False)])
def test_the_send_retries_up_to_the_configured_count(retries_so_far, retried):
    from app.tasks.email_tasks import send_candidate_email_task
    config = MagicMock()
    config.get_configs_by_keys.return_value = {"MAX_EMAIL_RETRY_COUNT": "4"}
    with _Harness() as h, patch(f"{MODULE}.ConfigRepository", return_value=config):
        notification = _make_notification()
        h.notification_repo.get_by_id.return_value = notification
        h.notification_repo.claim_for_sending.return_value = True
        h.template_repo.get_by_id.return_value = _make_template()
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = None
        throttled = ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "SendEmail")
        h.ses_client_instance.send_email.side_effect = EmailDeliveryException("SES send_email failed", throttled)
        send_candidate_email_task.push_request(retries=retries_so_far)
        try:
            if retried:
                with pytest.raises(EmailDeliveryException):
                    send_candidate_email_task(email_notification_id=str(notification.id))
                h.dead_letter_queue_repo.create.assert_not_called()
            else:
                send_candidate_email_task(email_notification_id=str(notification.id))
                h.dead_letter_queue_repo.create.assert_called_once()
        finally:
            send_candidate_email_task.pop_request()
