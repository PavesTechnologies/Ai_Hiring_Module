from app.models.async_tasks import FailureClassification


def classify(exc: Exception) -> FailureClassification:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        # This list is intentionally narrow today; the Gemini SDK exception
        # hierarchy will be expanded once the SDK is actually installed.
        return FailureClassification.TRANSIENT

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return FailureClassification.PERMANENT

    return FailureClassification.UNKNOWN
