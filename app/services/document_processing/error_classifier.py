from botocore.exceptions import ClientError
from sqlalchemy.exc import OperationalError

from app.models.async_tasks import FailureClassification

# M07-E03 S02 T03: SES error codes that are worth retrying (rate limiting/
# transient service issues) - everything else from SES (bad recipient,
# unverified sender, etc.) is a business failure and must not retry.
_TRANSIENT_SES_ERROR_CODES = {"Throttling", "ThrottlingException", "ServiceUnavailable", "RequestTimeout"}


def classify(exc: Exception) -> FailureClassification:
    if isinstance(exc, (ConnectionError, TimeoutError, OperationalError)):
        # OperationalError covers psycopg2/SQLAlchemy connection failures
        # (pool exhaustion, dropped connections, DB unreachable) - these are
        # transient by nature and should back off and retry rather than
        # dead-letter immediately. This list is intentionally narrow
        # otherwise; the Gemini SDK exception hierarchy will be expanded
        # once the SDK is actually installed.
        return FailureClassification.TRANSIENT

    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _TRANSIENT_SES_ERROR_CODES:
            return FailureClassification.TRANSIENT
        return FailureClassification.PERMANENT

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return FailureClassification.PERMANENT

    return FailureClassification.UNKNOWN
