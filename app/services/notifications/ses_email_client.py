import logging
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from uuid import uuid4

import boto3

from app.core.config import settings
from app.core.safe_errors import safe_error
from app.exceptions.email_exception import EmailDeliveryException


logger = logging.getLogger(__name__)

LOG_ONLY_PREFIX = "log-only-"
SMTP_PREFIX = "smtp-"
# The From address for local Mailpit mail when SES_FROM_EMAIL is unset (.invalid never resolves).
LOCAL_FROM = "airs-dev@mailpit.invalid"
SMTP_TIMEOUT_SECONDS = 10


def delivery_mode() -> str:
    """"ses", "smtp" (a local Mailpit; config.py refuses any other host) or "log" - anything else is log."""
    mode = (settings.email_delivery or "").strip().lower()
    return mode if mode in ("ses", "smtp") else "log"


def email_delivery_is_log() -> bool:
    """EMAIL_DELIVERY=log: emails are rendered and logged, never sent.

    Only an explicit "ses" or "smtp" delivers; any other value is treated as log-only."""
    return delivery_mode() == "log"


class SMTPNotSent(Exception):
    """The local SMTP server certainly did not take the email (refused the connection, the sender, the
    recipient or the message): a retry cannot send it twice. Wraps the smtplib error."""


# Raised before the server accepted the message: certainly not sent. A dropped connection or a timeout
# after the message data went out (SMTPServerDisconnected, socket.timeout) may have delivered it.
_SMTP_NOT_SENT = (ConnectionRefusedError, socket.gaierror, smtplib.SMTPConnectError, smtplib.SMTPHeloError,
                  smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError,
                  smtplib.SMTPNotSupportedError)


class SESEmailClient:
    """
    The one email sender: AWS SES, or under EMAIL_DELIVERY=smtp a local Mailpit, or log-only.
    Thin wrapper around AWS SES - mirrors StorageService's role (infra-only,
    no business/domain logic). Reuses the same AWS credentials/region
    already configured for S3 in app.core.config.settings; only the
    verified sender address (ses_from_email) is SES-specific.

    One email path: only notifications.send_candidate_email (app/tasks/email_tasks.py) may use
    this - every email is an email_notifications row first (tests/test_no_email_paths.py checks).
    """

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            if delivery_mode() != "ses" or not (settings.ses_from_email or "").strip():
                # ValueError: classified permanent, so it dead-letters instead of retrying.
                reason = ValueError("SES client refused: EMAIL_DELIVERY is not 'ses' or SES_FROM_EMAIL is unset")
                raise EmailDeliveryException(str(reason), reason)
            self._client = boto3.client(
                "ses",
                aws_access_key_id=settings.aws_access_key_id.get_secret_value(),
                aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
                region_name=settings.aws_region,
            )
        return self._client

    def send_email(self, *, to_address: str, subject: str, body_text: str, is_html: bool = False) -> str:
        """
        Returns the SES MessageId on success. Raises EmailDeliveryException
        (wrapping the original botocore exception) on failure - the caller
        classifies transient vs. permanent and decides whether to retry.

        email_tasks.py passes is_html=True: EmailTemplate.body_template is HTML.
        """
        mode = delivery_mode()
        if mode == "log":
            # Isolated local stack: nothing leaves the machine - no SES client
            # is ever created. Logged so tests can see what would have gone out.
            message_id = f"{LOG_ONLY_PREFIX}{uuid4()}"
            # Review item 28: no recipient address or subject (it carries the candidate's name).
            logger.warning("EMAIL NOT SENT (EMAIL_DELIVERY=log) | id=%s", message_id)
            return message_id
        if mode == "smtp":
            return self._send_smtp(to_address=to_address, subject=subject, body_text=body_text, is_html=is_html)
        body_key = "Html" if is_html else "Text"
        try:
            response = self._get_client().send_email(
                Source=settings.ses_from_email,
                Destination={"ToAddresses": [to_address]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {body_key: {"Data": body_text, "Charset": "UTF-8"}},
                },
            )
            return response["MessageId"]
        except Exception as exc:
            # Review item 28: SES errors can echo the recipient - the message carries the error class
            # and code only; the raw exception stays on .original for classification.
            raise EmailDeliveryException(f"SES send_email failed: {safe_error(exc)}", exc) from exc

    def _send_smtp(self, *, to_address: str, subject: str, body_text: str, is_html: bool) -> str:
        """
        EMAIL_DELIVERY=smtp: hands the email to the local Mailpit (SMTP_HOST is checked at startup to be
        local; no login, no TLS). Returns "smtp-<Message-ID>". Raises EmailDeliveryException wrapping
        SMTPNotSent when the server certainly did not take it (safe to retry), or the raw error when it
        may have (the send task then never sends it again).
        """
        host = (settings.smtp_host or "").strip()
        message = EmailMessage()
        message["From"] = formataddr(("AIRS", (settings.ses_from_email or "").strip() or LOCAL_FROM))
        message["To"] = to_address
        message["Subject"] = subject
        message_id = make_msgid(domain="airs.local")
        message["Message-ID"] = message_id
        message.set_content(body_text, subtype="html" if is_html else "plain")
        try:
            with smtplib.SMTP(host, int(settings.smtp_port), timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.send_message(message)
        except _SMTP_NOT_SENT as exc:
            reason = SMTPNotSent(safe_error(exc))
            raise EmailDeliveryException(f"SMTP send failed (not sent): {safe_error(exc)}", reason) from exc
        except Exception as exc:
            raise EmailDeliveryException(f"SMTP send failed (outcome unknown): {safe_error(exc)}", exc) from exc
        return f"{SMTP_PREFIX}{message_id.strip('<>')}"
