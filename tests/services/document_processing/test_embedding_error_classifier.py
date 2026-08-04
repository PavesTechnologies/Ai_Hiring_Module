from types import SimpleNamespace

from app.services.document_processing.embedding_error_classifier import (
    EmbeddingFailureClassification,
    classify_embedding_error,
)


class _HTTPStatusError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


def test_429_classifies_as_rate_limited_with_retry_after():
    exc = _HTTPStatusError(429, headers={"Retry-After": "12"})

    result = classify_embedding_error(exc)

    assert result.classification == EmbeddingFailureClassification.RATE_LIMITED
    assert result.retry_after_seconds == 12.0


def test_429_without_retry_after_header_still_rate_limited_but_no_delay():
    exc = _HTTPStatusError(429)

    result = classify_embedding_error(exc)

    assert result.classification == EmbeddingFailureClassification.RATE_LIMITED
    assert result.retry_after_seconds is None


def test_429_with_non_numeric_retry_after_ignored():
    exc = _HTTPStatusError(429, headers={"Retry-After": "not-a-number"})

    result = classify_embedding_error(exc)

    assert result.retry_after_seconds is None


def test_500_classifies_as_server_error():
    result = classify_embedding_error(_HTTPStatusError(500))
    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR


def test_503_classifies_as_server_error():
    result = classify_embedding_error(_HTTPStatusError(503))
    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR


def test_400_classifies_as_permanent():
    result = classify_embedding_error(_HTTPStatusError(400))
    assert result.classification == EmbeddingFailureClassification.PERMANENT


def test_status_code_directly_on_exception_also_recognized():
    exc = Exception("boom")
    exc.status_code = 503

    result = classify_embedding_error(exc)

    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR


def test_exception_with_no_http_status_falls_back_to_server_error():
    """Today's local SentenceTransformer call raises plain exceptions - no .response/.status_code."""
    result = classify_embedding_error(RuntimeError("model crashed"))
    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR


def test_value_error_falls_back_to_permanent_via_generic_classifier():
    """ValueError is PERMANENT in the generic classify() - folded through unchanged."""
    result = classify_embedding_error(ValueError("bad input"))
    assert result.classification == EmbeddingFailureClassification.PERMANENT


def test_connection_error_falls_back_to_server_error():
    """ConnectionError is TRANSIENT in the generic classify() - folded onto SERVER_ERROR's backoff bucket."""
    result = classify_embedding_error(ConnectionError("network blip"))
    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR


def test_unrecognized_http_status_falls_back_to_generic_classifier():
    """A status code that isn't 429/500/503/400 (e.g. 404) isn't specially handled - falls through to the generic path."""
    result = classify_embedding_error(_HTTPStatusError(404))
    assert result.classification == EmbeddingFailureClassification.SERVER_ERROR
