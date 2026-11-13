"""
EMAIL_DELIVERY=smtp: a local Mailpit only (dev docker-compose, the e2e stack). Nothing leaves the machine:
the host must be local, and production sends only through SES. These tests never open a socket - smtplib
is replaced; the e2e stack checks a real Mailpit.
"""
import smtplib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.config import SMTP_LOCAL_HOSTS, Settings
from app.exceptions.email_exception import EmailDeliveryException
from app.services.notifications import ses_email_client as client_module
from app.services.notifications.ses_email_client import SESEmailClient, SMTPNotSent


# --- settings ------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", sorted(SMTP_LOCAL_HOSTS) + [" Localhost "])
def test_smtp_to_a_local_mailpit_is_accepted(host):
    settings = Settings(email_delivery="smtp", smtp_host=host, smtp_port=1025)
    assert settings.email_delivery == "smtp"


@pytest.mark.parametrize("host", ["smtp.gmail.com", "email-smtp.ap-south-1.amazonaws.com", "10.0.0.5", "", "mailpit.example.com"])
def test_smtp_to_anything_but_a_local_mailpit_is_refused(host):
    with pytest.raises(ValidationError, match="local Mailpit only"):
        Settings(email_delivery="smtp", smtp_host=host)


@pytest.mark.parametrize("mode", ["log", "smtp"])
def test_production_refuses_log_and_smtp(mode):
    with pytest.raises(ValidationError, match="APP_ENV=production needs EMAIL_DELIVERY=ses"):
        Settings(app_env="production", jwt_issuer="https://ums.example.invalid", email_delivery=mode,
                 smtp_host="localhost")


def test_production_accepts_ses():
    settings = Settings(app_env="production", jwt_issuer="https://ums.example.invalid", email_delivery="ses",
                        ses_from_email="hr@example.invalid")
    assert settings.email_delivery == "ses"


def test_the_production_check_does_not_hide_a_misspelt_delivery():
    with pytest.raises(ValidationError, match="EMAIL_DELIVERY must be"):
        Settings(app_env="production", jwt_issuer="https://ums.example.invalid", email_delivery="sess")


# --- the sender ----------------------------------------------------------------------------------

def _smtp_settings(**values):
    base = dict(email_delivery="smtp", smtp_host="mailpit", smtp_port=1025, ses_from_email="",
                aws_access_key_id="", aws_secret_access_key="", aws_region="ap-south-1")
    return patch.object(client_module, "settings", SimpleNamespace(**{**base, **values}))


def _fake_smtp(send_error=None):
    server = MagicMock()
    server.__enter__.return_value = server
    if send_error is not None:
        server.send_message.side_effect = send_error
    return server


def test_smtp_hands_the_email_to_the_local_host_and_never_creates_an_ses_client():
    server = _fake_smtp()
    with _smtp_settings(), patch("smtplib.SMTP", return_value=server) as smtp, \
            patch("boto3.client", side_effect=AssertionError("SES client created")):
        message_id = SESEmailClient().send_email(to_address="cand@example.invalid", subject="Interview",
                                                 body_text="<p>Hi</p>", is_html=True)
    smtp.assert_called_once_with("mailpit", 1025, timeout=client_module.SMTP_TIMEOUT_SECONDS)
    message = server.send_message.call_args.args[0]
    assert message["To"] == "cand@example.invalid" and message["Subject"] == "Interview"
    assert client_module.LOCAL_FROM in message["From"]
    assert message.get_content_type() == "text/html"
    assert message_id.startswith(client_module.SMTP_PREFIX) and message_id[len(client_module.SMTP_PREFIX):] in message["Message-ID"]


def test_smtp_uses_the_configured_sender_when_there_is_one():
    server = _fake_smtp()
    with _smtp_settings(ses_from_email="hr@example.invalid"), patch("smtplib.SMTP", return_value=server):
        SESEmailClient().send_email(to_address="cand@example.invalid", subject="s", body_text="b")
    assert "hr@example.invalid" in server.send_message.call_args.args[0]["From"]


@pytest.mark.parametrize("error", [
    ConnectionRefusedError(),
    smtplib.SMTPConnectError(421, b"busy"),
    smtplib.SMTPRecipientsRefused({"cand@example.invalid": (550, b"no")}),
    smtplib.SMTPSenderRefused(550, b"no", "airs@x.invalid"),
    smtplib.SMTPDataError(554, b"rejected"),
])
def test_a_refusal_before_the_server_took_it_is_not_sent_and_safe_to_retry(error):
    from app.tasks.email_tasks import _not_sent
    server = _fake_smtp(send_error=error)
    with _smtp_settings(), patch("smtplib.SMTP", return_value=server):
        with pytest.raises(EmailDeliveryException) as raised:
            SESEmailClient().send_email(to_address="cand@example.invalid", subject="s", body_text="b")
    assert isinstance(raised.value.original, SMTPNotSent)
    assert _not_sent(raised.value)
    assert "cand@example.invalid" not in str(raised.value)          # no recipient in the error


def test_a_refused_connection_is_not_sent():
    from app.tasks.email_tasks import _not_sent
    with _smtp_settings(), patch("smtplib.SMTP", side_effect=ConnectionRefusedError()):
        with pytest.raises(EmailDeliveryException) as raised:
            SESEmailClient().send_email(to_address="cand@example.invalid", subject="s", body_text="b")
    assert _not_sent(raised.value)


@pytest.mark.parametrize("error", [smtplib.SMTPServerDisconnected("gone"), TimeoutError()])
def test_a_drop_after_the_message_went_out_is_outcome_unknown(error):
    from app.tasks.email_tasks import _not_sent
    server = _fake_smtp(send_error=error)
    with _smtp_settings(), patch("smtplib.SMTP", return_value=server):
        with pytest.raises(EmailDeliveryException) as raised:
            SESEmailClient().send_email(to_address="cand@example.invalid", subject="s", body_text="b")
    assert not _not_sent(raised.value)            # never sent again automatically


def test_the_ses_client_is_still_refused_in_smtp_mode():
    with _smtp_settings(ses_from_email="hr@example.invalid"), \
            patch("boto3.client", side_effect=AssertionError("SES client created")):
        with pytest.raises(EmailDeliveryException):
            SESEmailClient()._get_client()
