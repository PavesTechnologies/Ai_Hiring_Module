import logging
from uuid import uuid4

import boto3

from app.core.config import settings
from app.exceptions.email_exception import EmailDeliveryException


logger = logging.getLogger(__name__)

LOG_ONLY_PREFIX = "log-only-"


def email_delivery_is_log() -> bool:
    """EMAIL_DELIVERY=log: emails are rendered and logged, never sent.

    Only an explicit "ses" sends; any other value is treated as log-only."""
    return (settings.email_delivery or "").strip().lower() != "ses"


class SESEmailClient:
    """
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
            if email_delivery_is_log() or not (settings.ses_from_email or "").strip():
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
        if email_delivery_is_log():
            # Isolated local stack: nothing leaves the machine - no SES client
            # is ever created. Logged so tests can see what would have gone out.
            message_id = f"{LOG_ONLY_PREFIX}{uuid4()}"
            logger.warning("EMAIL NOT SENT (EMAIL_DELIVERY=log) | id=%s to=%s subject=%s",
                           message_id, to_address, subject)
            return message_id
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
            raise EmailDeliveryException(f"SES send_email failed: {exc}", exc) from exc
