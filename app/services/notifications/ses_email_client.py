import logging
from uuid import uuid4

import boto3

from app.core.config import settings
from app.exceptions.email_exception import EmailDeliveryException


logger = logging.getLogger(__name__)


class SESEmailClient:
    """
    Thin wrapper around AWS SES - mirrors StorageService's role (infra-only,
    no business/domain logic). Reuses the same AWS credentials/region
    already configured for S3 in app.core.config.settings; only the
    verified sender address (ses_from_email) is SES-specific.
    """

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "ses",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region,
            )
        return self._client

    def send_email(self, *, to_address: str, subject: str, body_text: str, is_html: bool = False) -> str:
        """
        Returns the SES MessageId on success. Raises EmailDeliveryException
        (wrapping the original botocore exception) on failure - the caller
        classifies transient vs. permanent and decides whether to retry.

        HTML template design fix: is_html defaults to False so the
        existing plain-text-only caller (embedding_health_tasks.py's
        ops alert, which isn't built from an EmailTemplate row and was
        never meant to be HTML) is unaffected. email_tasks.py passes
        True since EmailTemplate.body_template is now HTML.
        """
        if (settings.email_delivery or "ses").lower() == "log":
            # Isolated local stack: nothing leaves the machine - no SES client
            # is ever created. Logged so tests can see what would have gone out.
            message_id = f"log-only-{uuid4()}"
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
