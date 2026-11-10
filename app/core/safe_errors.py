"""
Error text safe to store or log (review item 28).

Exception messages can carry personal data: the email provider echoes the recipient, a pydantic error
quotes the model's extraction of a resume (input_value=...), a storage error names a path, a database
error carries row values. Task logs, dead letters, stage-failure rows, email error_reason and log lines
get the exception's class and, where the provider gives one, its error code - never its message.
The raw exception stays available in memory (exc / exc.original) for code that needs to classify it.
"""


def error_code(exc: BaseException) -> str | None:
    """A provider error code, when there is one (botocore ClientError, HTTP status)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, str)) and str(value) and len(str(value)) <= 40:
            return str(value)
    return None


def safe_error(exc: BaseException | None, context: str | None = None) -> str:
    """
    "ClassName" or "ClassName (code)" - for the innermost cause too, when the exception wraps one
    (e.g. EmailDeliveryException wrapping a ClientError). context is caller-supplied, ids only.
    """
    if exc is None:
        return context or "error"
    parts = [_describe(exc)]
    original = getattr(exc, "original", None) or exc.__cause__
    if isinstance(original, BaseException) and original is not exc:
        parts.append(_describe(original))
    text = " <- ".join(parts)
    return f"{context}: {text}" if context else text


def _describe(exc: BaseException) -> str:
    code = error_code(exc)
    return f"{type(exc).__name__} ({code})" if code else type(exc).__name__


class SafeRetryError(Exception):
    """Handed to Celery's retry(exc=...) so its "Retry in Ns: <exc>" log line carries no personal data."""
