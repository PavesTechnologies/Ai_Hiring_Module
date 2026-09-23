from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class LLMProviderName:
    """
    Values stored in ai_provider_config.provider. Plain strings (varchar
    column, not a DB enum) - same reasoning as UserOAuthToken.provider:
    adding a provider shouldn't need a migration.
    """
    GOOGLE = "GOOGLE"
    ANTHROPIC = "ANTHROPIC"
    OPENAI = "OPENAI"
    GROQ = "GROQ"

    ALL = (GOOGLE, ANTHROPIC, OPENAI, GROQ)

    LABELS = {
        GOOGLE: "Google Gemini",
        ANTHROPIC: "Anthropic Claude",
        OPENAI: "OpenAI",
        GROQ: "Groq",
    }


class LLMError(Exception):
    """
    Provider-neutral failure raised by every LLMProvider. Each adapter maps
    its own SDK's exception hierarchy onto the two subclasses below, so
    error_classifier.classify (and therefore RetryDriver) can decide
    retry-vs-dead-letter without knowing which SDK produced the error.
    `status_code` is the provider's HTTP status when there was one.
    `reason` (an LLMErrorReason) is what the Settings UI turns into a
    user-facing sentence - the message itself is raw SDK detail for logs
    only and must never be shown to a user.
    """

    def __init__(self, message: str, status_code: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason or reason_for(status_code, message)


class LLMTransientError(LLMError):
    """Worth retrying: 5xx/overloaded, timeouts, connection drops, short-term rate limiting."""


class LLMPermanentError(LLMError):
    """
    Fails identically on every attempt: bad/unauthorised API key, unknown
    model, malformed request, exhausted quota/billing, unparseable output.
    """


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str


class LLMProvider(Protocol):
    provider_name: str
    model: str

    def generate_json(self, prompt: str, response_schema: type[BaseModel]) -> dict:
        """
        Sends one fully-composed prompt and returns the model's JSON
        payload as a dict, unvalidated - callers validate against their
        own stricter *Response models, exactly as they did with Gemini.
        """
        ...

    def list_models(self) -> list[ModelInfo]:
        ...


class LLMErrorReason:
    INVALID_KEY = "INVALID_KEY"
    NO_ACCESS = "NO_ACCESS"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    BAD_OUTPUT = "BAD_OUTPUT"
    REFUSED = "REFUSED"
    BAD_REQUEST = "BAD_REQUEST"
    UNKNOWN = "UNKNOWN"


_QUOTA_EXHAUSTED_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "exceeded your current quota",
    "insufficient_quota",
    "billing",
    "credit balance",
)

# Providers don't agree on the status code for a bad key - Gemini sends
# 400 INVALID_ARGUMENT / API_KEY_INVALID, the others 401 - so the message
# is checked too.
_INVALID_KEY_MARKERS = (
    "API_KEY_INVALID",
    "API key not valid",
    "invalid x-api-key",
    "invalid_api_key",
    "Incorrect API key",
    "Invalid API Key",
    "authentication_error",
    "API key expired",
)

_MODEL_NOT_FOUND_MARKERS = (
    "model_not_found",
    "is not found for API version",
    "does not exist",
    "not_found_error",
    "is not supported for generateContent",
    "decommissioned",
)


def _contains_any(message: str, markers: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(marker.lower() in lowered for marker in markers)


# Gemini words a per-minute limit exactly like a spent quota ("You exceeded
# your current quota, please check your plan and billing details") - the
# difference is only in the quota id / retry hint, e.g.
#   quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier
#   "Please retry in 8.39s."
# Those clear on their own within seconds, so they're rate limiting, not
# exhausted quota. Per-day limits stay "exhausted" (they last until tomorrow).
_SHORT_TERM_LIMIT_MARKERS = (
    "PerMinute",
    "per minute",
    "retry in",
    "retryDelay",
)


def is_quota_exhausted(message: str) -> bool:
    """
    Every provider returns 429 for both short-term rate limiting (transient)
    and spent quota/billing (permanent until someone changes the plan); only
    the message tells them apart.
    """
    if _contains_any(message, _SHORT_TERM_LIMIT_MARKERS) and "PerDay" not in message:
        return False
    return _contains_any(message, _QUOTA_EXHAUSTED_MARKERS)


def reason_for(status_code: int | None, message: str) -> str:
    if _contains_any(message, _INVALID_KEY_MARKERS) or status_code == 401:
        return LLMErrorReason.INVALID_KEY
    if is_quota_exhausted(message):
        return LLMErrorReason.QUOTA_EXHAUSTED
    if _contains_any(message, _MODEL_NOT_FOUND_MARKERS) or status_code == 404:
        return LLMErrorReason.MODEL_NOT_FOUND
    if status_code == 403:
        return LLMErrorReason.NO_ACCESS
    if status_code == 429:
        return LLMErrorReason.RATE_LIMITED
    if status_code is None or status_code >= 500:
        return LLMErrorReason.UNAVAILABLE
    if 400 <= status_code < 500:
        return LLMErrorReason.BAD_REQUEST
    return LLMErrorReason.UNKNOWN


def status_error(status_code: int | None, message: str) -> LLMError:
    """Shared HTTP-status -> neutral-error mapping for every provider SDK."""
    if status_code is None or status_code >= 500:
        error_cls = LLMTransientError
    elif status_code == 429:
        error_cls = LLMPermanentError if is_quota_exhausted(message) else LLMTransientError
    else:
        error_cls = LLMPermanentError
    return error_cls(message, status_code=status_code)
