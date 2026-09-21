from botocore.exceptions import ClientError
from google.genai.errors import APIError as GenAIAPIError
from google.genai.errors import ClientError as GenAIClientError
from google.genai.errors import ServerError as GenAIServerError
from sqlalchemy.exc import OperationalError

from app.models.async_tasks import FailureClassification

# Gemini returns 429 for two different things, and only one is worth
# retrying:
#   - short-term rate limiting (too many requests just now) — transient,
#     it clears on its own;
#   - RESOURCE_EXHAUSTED, i.e. the account's quota/billing cap is spent —
#     permanent until somebody changes the plan. Retrying cannot succeed
#     and each attempt spends another call against an exhausted quota.
# The status code is identical; only the message distinguishes them.
_QUOTA_EXHAUSTED_MARKERS = ("RESOURCE_EXHAUSTED", "exceeded your current quota", "billing")

# Every other 4xx from the SDK (400 malformed request, 401/403 bad or
# unauthorised API key, 404 unknown model) fails identically on every
# attempt, so retrying only delays surfacing the real problem.
_TRANSIENT_GENAI_CLIENT_CODES = {429}


def _is_quota_exhausted(exc: Exception) -> bool:
    message = str(exc)
    return any(marker.lower() in message.lower() for marker in _QUOTA_EXHAUSTED_MARKERS)

# M07-E03 S02 T03: SES error codes that are worth retrying (rate limiting/
# transient service issues) - everything else from SES (bad recipient,
# unverified sender, etc.) is a business failure and must not retry.
_TRANSIENT_SES_ERROR_CODES = {"Throttling", "ThrottlingException", "ServiceUnavailable", "RequestTimeout"}


def classify(exc: Exception) -> FailureClassification:
    # Gemini first: GenAIServerError (5xx — 503 UNAVAILABLE when the model
    # is overloaded, the single most common failure in this pipeline) is
    # unambiguously transient. Previously these fell through to UNKNOWN,
    # which happened to retry (RetryDriver only dead-letters on PERMANENT)
    # but only by accident — nothing recorded the intent, and a bad API key
    # burned the full attempt budget before failing. Checked before the
    # botocore ClientError branch below because the two SDKs both export a
    # class named ClientError and these are the genai ones.
    if isinstance(exc, GenAIServerError):
        return FailureClassification.TRANSIENT

    if isinstance(exc, GenAIClientError):
        if getattr(exc, "code", None) in _TRANSIENT_GENAI_CLIENT_CODES:
            # Quota exhaustion dead-letters immediately instead of burning
            # the remaining attempts — the operator needs to see "quota
            # spent" now, not five failed retries from now.
            if _is_quota_exhausted(exc):
                return FailureClassification.PERMANENT
            return FailureClassification.TRANSIENT
        return FailureClassification.PERMANENT

    if isinstance(exc, (ConnectionError, TimeoutError, OperationalError)):
        # OperationalError covers psycopg2/SQLAlchemy connection failures
        # (pool exhaustion, dropped connections, DB unreachable) - these are
        # transient by nature and should back off and retry rather than
        # dead-letter immediately. The Gemini SDK hierarchy this comment
        # used to defer is now handled explicitly above.
        return FailureClassification.TRANSIENT

    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _TRANSIENT_SES_ERROR_CODES:
            return FailureClassification.TRANSIENT
        return FailureClassification.PERMANENT

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return FailureClassification.PERMANENT

    # Any other genai APIError shape the SDK may add: unknown rather than
    # permanent, so RetryDriver still gives it its attempts instead of
    # dead-lettering a failure we have not characterised.
    if isinstance(exc, GenAIAPIError):
        return FailureClassification.UNKNOWN

    return FailureClassification.UNKNOWN
