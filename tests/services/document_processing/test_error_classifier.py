from botocore.exceptions import ClientError

from app.models.async_tasks import FailureClassification
from app.services.document_processing.error_classifier import classify

"""
M07-E03 S02 T03: classify() extended to recognize botocore ClientError
(SES) - transient AWS-side issues (throttling) are retryable, everything
else from SES (bad recipient, unverified sender, etc.) is a business
failure that must not retry forever.
"""


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "SendEmail")


def test_throttling_is_transient():
    assert classify(_client_error("Throttling")) == FailureClassification.TRANSIENT


def test_service_unavailable_is_transient():
    assert classify(_client_error("ServiceUnavailable")) == FailureClassification.TRANSIENT


def test_message_rejected_is_permanent():
    assert classify(_client_error("MessageRejected")) == FailureClassification.PERMANENT


def test_unrecognized_ses_error_code_defaults_to_permanent():
    """Safer default: an unknown SES error code should not retry forever."""
    assert classify(_client_error("SomeNewErrorCode")) == FailureClassification.PERMANENT


def test_existing_connection_error_classification_unchanged():
    assert classify(ConnectionError()) == FailureClassification.TRANSIENT


def test_existing_value_error_classification_unchanged():
    assert classify(ValueError()) == FailureClassification.PERMANENT
