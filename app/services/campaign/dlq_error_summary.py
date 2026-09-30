"""
One short, plain phrase for a dead-lettered task's error, for the UI. The
raw `final_error_message` (full provider reply / exception text) stays on
the DLQ row for logs and audit; the UI shows only this.
"""
import re

from app.services.llm.base import LLMErrorReason, reason_for

_MAX_LEN = 90

# Every LLM provider adapter words its failures with one of these prefixes
# (gemini/anthropic/openai/groq providers) - only those are classified via
# reason_for; anything else is a plain exception message.
_LLM_API_ERROR = re.compile(r"\bAPI error:\s*(?:Error code:\s*)?(\d{3})?", re.IGNORECASE)
_LLM_CONNECTION_ERROR = re.compile(r"\bconnection error\b", re.IGNORECASE)
_LLM_BAD_OUTPUT = re.compile(
    r"returned invalid JSON|does not match the schema|no structured output|was truncated|was cut off",
    re.IGNORECASE,
)
_LLM_REFUSED = re.compile(r"declined the request", re.IGNORECASE)

_REASON_SUMMARY = {
    LLMErrorReason.DAILY_LIMIT: "AI provider daily limit reached",
    LLMErrorReason.RATE_LIMITED: "AI provider rate limit hit",
    LLMErrorReason.CREDITS_EXHAUSTED: "AI provider account is out of credits",
    LLMErrorReason.QUOTA_EXHAUSTED: "AI provider quota used up",
    LLMErrorReason.INVALID_KEY: "AI provider API key is invalid",
    LLMErrorReason.NO_ACCESS: "API key has no access to this AI model",
    LLMErrorReason.MODEL_NOT_FOUND: "AI model is not available",
    LLMErrorReason.UNAVAILABLE: "AI provider was unavailable",
    LLMErrorReason.BAD_OUTPUT: "AI returned an unusable response",
    LLMErrorReason.REFUSED: "AI model declined the request",
    LLMErrorReason.BAD_REQUEST: "AI provider rejected the request",
}
_GENERIC = "Processing failed"


def _first_sentence(message: str) -> str:
    text = message.strip().splitlines()[0].strip()
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return text if len(text) <= _MAX_LEN else text[: _MAX_LEN - 1].rstrip() + "…"


def summarize_dlq_error(message: str | None) -> str:
    if not message or not message.strip():
        return _GENERIC

    if _LLM_REFUSED.search(message):
        return _REASON_SUMMARY[LLMErrorReason.REFUSED]
    if _LLM_BAD_OUTPUT.search(message):
        return _REASON_SUMMARY[LLMErrorReason.BAD_OUTPUT]
    if _LLM_CONNECTION_ERROR.search(message) or "timed out" in message.lower():
        return _REASON_SUMMARY[LLMErrorReason.UNAVAILABLE]

    api_error = _LLM_API_ERROR.search(message)
    if api_error:
        status_code = int(api_error.group(1)) if api_error.group(1) else None
        return _REASON_SUMMARY.get(reason_for(status_code, message), "AI provider error")

    return _first_sentence(message)
