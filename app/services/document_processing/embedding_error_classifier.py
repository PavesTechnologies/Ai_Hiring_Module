import enum
from dataclasses import dataclass

from app.models.async_tasks import FailureClassification
from app.services.document_processing.error_classifier import classify


class EmbeddingFailureClassification(enum.Enum):
    """
    HTTP-status-aware classification for EMBED_RESUME's embedding-service
    call, distinct from the generic exception-type-only FailureClassification
    (TRANSIENT/PERMANENT/UNKNOWN) other tasks use.

    Today's EmbeddingService calls a local SentenceTransformer model
    in-process - there is no HTTP request, so it can never actually raise a
    429/500/503/400. This classifier is written generically anyway, so it
    is already correct the day this becomes a real HTTP-based embedding
    API: RATE_LIMITED/SERVER_ERROR/PERMANENT only ever result from an
    exception that actually carries an HTTP status code (checked via
    _extract_http_status_code below); everything else - including every
    exception the local model raises today - falls through to
    SERVER_ERROR (the generic exponential-backoff bucket), unless the
    existing generic classify() already calls it PERMANENT (ValueError/
    KeyError/TypeError).
    """
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class EmbeddingFailureInfo:
    classification: EmbeddingFailureClassification
    # Only ever set for RATE_LIMITED, and only when the exception actually
    # carried a Retry-After header - None means "no header, use the
    # standard exponential backoff for this attempt instead."
    retry_after_seconds: float | None = None


def classify_embedding_error(exc: Exception) -> EmbeddingFailureInfo:
    status_code = _extract_http_status_code(exc)

    if status_code == 429:
        return EmbeddingFailureInfo(
            EmbeddingFailureClassification.RATE_LIMITED,
            retry_after_seconds=_extract_retry_after_seconds(exc),
        )
    if status_code in (500, 503):
        return EmbeddingFailureInfo(EmbeddingFailureClassification.SERVER_ERROR)
    if status_code == 400:
        return EmbeddingFailureInfo(EmbeddingFailureClassification.PERMANENT)

    # No HTTP status available - true for every exception the local
    # SentenceTransformer model raises today. Fall back to the existing
    # generic exception-type classifier, folding its TRANSIENT/UNKNOWN
    # outcomes onto SERVER_ERROR's exponential-backoff bucket, so every
    # non-HTTP failure still gets a sensible retry policy.
    if classify(exc) == FailureClassification.PERMANENT:
        return EmbeddingFailureInfo(EmbeddingFailureClassification.PERMANENT)
    return EmbeddingFailureInfo(EmbeddingFailureClassification.SERVER_ERROR)


def _extract_http_status_code(exc: Exception) -> int | None:
    """
    Covers both requests.HTTPError/httpx.HTTPStatusError style
    (exc.response.status_code) and SDKs that expose .status_code directly
    on the exception itself. Returns None for anything else - which is
    every exception type the local embedding model can currently raise.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    return None


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None

    retry_after = headers.get("Retry-After")
    if retry_after is None:
        return None

    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None
