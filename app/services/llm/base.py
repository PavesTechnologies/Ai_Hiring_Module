import re
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
        # Details read from the provider's reply, so the user-facing message
        # can say how long to wait and which limit was hit.
        self.retry_after_seconds = retry_after_seconds(message)
        self.quota_limit = quota_limit(message)


class LLMTransientError(LLMError):
    """Worth retrying: 5xx/overloaded, timeouts, connection drops, per-minute rate limiting."""


class LLMPermanentError(LLMError):
    """
    Fails identically on every attempt: bad/unauthorised API key, unknown
    model, malformed request, daily limit / no credits, unparseable output.
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
    """
    Stable error codes. The Settings UI shows the code next to the message,
    so keep the values stable.
    """
    INVALID_KEY = "INVALID_KEY"              # key wrong, revoked or expired
    NO_ACCESS = "NO_ACCESS"                  # key valid, but not allowed this provider/model
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"      # model id unknown to this key
    RATE_LIMITED = "RATE_LIMITED"            # per-minute limit - clears within seconds
    DAILY_LIMIT = "DAILY_LIMIT"              # per-day limit (e.g. Gemini free tier) - clears tomorrow
    CREDITS_EXHAUSTED = "CREDITS_EXHAUSTED"  # account has no credits / spending cap reached
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"      # quota used up; provider didn't say which window
    UNAVAILABLE = "UNAVAILABLE"              # provider down/overloaded, timeout, network
    BAD_OUTPUT = "BAD_OUTPUT"                # model can't return the required JSON
    REFUSED = "REFUSED"                      # model declined the request
    BAD_REQUEST = "BAD_REQUEST"              # provider rejected the request
    UNKNOWN = "UNKNOWN"

    # These fail the same way on an immediate retry - never worth retrying.
    PERMANENT = frozenset({
        INVALID_KEY, NO_ACCESS, MODEL_NOT_FOUND, DAILY_LIMIT, CREDITS_EXHAUSTED,
        QUOTA_EXHAUSTED, BAD_OUTPUT, REFUSED, BAD_REQUEST,
    })


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

# Out of money rather than out of requests:
#   OpenAI    429 insufficient_quota / billing_hard_limit_reached
#   Anthropic 400 "Your credit balance is too low to access the Anthropic API"
_CREDITS_MARKERS = (
    "insufficient_quota",
    "billing_hard_limit",
    "credit balance",
    "insufficient credits",
    "payment required",
)

# Quota used up with no stated window. Gemini words *every* quota error
# like this ("You exceeded your current quota, please check your plan and
# billing details"), so the window checks in _quota_reason run first.
_QUOTA_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "exceeded your current quota",
    "quota exceeded",
)

# Gemini puts the window in the quota id (case-sensitive match):
#   GenerateRequestsPerMinutePerProjectPerModel-FreeTier
#   GenerateRequestsPerDayPerProjectPerModel-FreeTier
# OpenAI/Groq say "tokens per minute (TPM)" / "requests per day (RPD)".
_PER_DAY_IDS = ("PerDay", "(RPD)")
_PER_DAY_PHRASES = ("per day",)
_PER_MINUTE_IDS = ("PerMinute", "(RPM)", "(TPM)")
_PER_MINUTE_PHRASES = ("per minute", "per min")

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry in\s+([\d.]+)\s*s", re.IGNORECASE),
    re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE),
    re.compile(r"retryDelay\W+([\d.]+)s", re.IGNORECASE),
)
_QUOTA_LIMIT_PATTERN = re.compile(r"\blimit:\s*(\d+)", re.IGNORECASE)


def _contains_any(message: str, markers: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(marker.lower() in lowered for marker in markers)


def retry_after_seconds(message: str) -> float | None:
    """The provider's own "retry in N seconds" hint, when it gives one."""
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(message)
        if match:
            try:
                return float(match.group(1).rstrip("."))
            except ValueError:
                return None
    return None


def quota_limit(message: str) -> int | None:
    """The limit that was hit, e.g. Gemini's "limit: 5" (requests per window)."""
    match = _QUOTA_LIMIT_PATTERN.search(message)
    return int(match.group(1)) if match else None


def _quota_reason(message: str) -> str | None:
    """Which kind of limit a quota / rate-limit reply describes, if any."""
    if any(m in message for m in _PER_DAY_IDS) or _contains_any(message, _PER_DAY_PHRASES):
        return LLMErrorReason.DAILY_LIMIT
    if (
        any(m in message for m in _PER_MINUTE_IDS)
        or _contains_any(message, _PER_MINUTE_PHRASES)
        or retry_after_seconds(message) is not None
    ):
        return LLMErrorReason.RATE_LIMITED
    if _contains_any(message, _CREDITS_MARKERS):
        return LLMErrorReason.CREDITS_EXHAUSTED
    if _contains_any(message, _QUOTA_MARKERS):
        return LLMErrorReason.QUOTA_EXHAUSTED
    return None


def is_quota_exhausted(message: str) -> bool:
    """
    Every provider returns 429 for both short-term rate limiting (transient)
    and spent quota/credits (permanent until someone changes the plan); only
    the message tells them apart.
    """
    return _quota_reason(message) in (
        LLMErrorReason.DAILY_LIMIT, LLMErrorReason.CREDITS_EXHAUSTED, LLMErrorReason.QUOTA_EXHAUSTED,
    )


def reason_for(status_code: int | None, message: str) -> str:
    if _contains_any(message, _INVALID_KEY_MARKERS) or status_code == 401:
        return LLMErrorReason.INVALID_KEY
    # Quota/credit replies come back as 429 (all), 400 (Anthropic credits)
    # or 402/403 (billing) - never as a 5xx.
    quota = _quota_reason(message)
    if quota is not None and (status_code is None or status_code < 500):
        return quota
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
    """
    Shared HTTP-status -> neutral-error mapping for every provider SDK. The
    reason decides retry-vs-fail: a per-minute limit or an outage is retried,
    a daily limit or an empty account is not.
    """
    reason = reason_for(status_code, message)
    error_cls = LLMPermanentError if reason in LLMErrorReason.PERMANENT else LLMTransientError
    return error_cls(message, status_code=status_code, reason=reason)
