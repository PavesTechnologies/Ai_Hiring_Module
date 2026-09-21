"""
Turns a raw pipeline exception string into copy an HR user can act on.

Every error_message stored on celery_task_log / stage_failure_logs is
str(exception) — useful in a log, wrong in a UI. A recruiter checking
"my uploads" currently reads things like:

    503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is
    currently experiencing high demand...', 'status': 'UNAVAILABLE'}}

which tells them nothing they can decide on, and leaks the AI provider
and our internal schema names. This maps the failure shapes the JD/resume
pipelines actually produce onto a short sentence plus, where one exists,
the action the user can take. The raw string is never discarded — callers
expose it alongside, under a separate field, for support/debugging.

Matching is on the stringified error rather than exception types on
purpose: these strings are read back out of the database long after the
original exception object is gone, so type-based dispatch (as in
error_classifier.classify, which runs on the live exception) isn't
available here. Keep the patterns narrow and ordered most-specific-first;
anything unmatched falls back to a generic message rather than guessing.
"""

import re

_FALLBACK = "Processing failed unexpectedly. Please try again, or contact support if it keeps happening."

# (compiled pattern, user-facing message). Order matters — first match
# wins, so specific provider/DB errors must precede broad ones.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\b503\b|UNAVAILABLE|high demand|overloaded", re.I),
        "The AI service is temporarily busy. This usually clears on its own — retry in a few minutes.",
    ),
    (
        re.compile(r"\b429\b|rate.?limit|quota|RESOURCE_EXHAUSTED", re.I),
        "The AI service rate limit was reached. Please wait a few minutes and retry.",
    ),
    (
        re.compile(r"\b(504|timeout|timed out|deadline exceeded)\b", re.I),
        "The document took too long to process. Try again, or re-upload a smaller file.",
    ),
    (
        re.compile(r"\b(401|403|permission denied|unauthenticated|api key)\b", re.I),
        "The AI service rejected our credentials. This needs an administrator — please contact support.",
    ),
    (
        re.compile(r"NotNullViolation|violates not-null constraint", re.I),
        "A required field was missing from this upload. Please re-submit the form with every field filled in.",
    ),
    (
        re.compile(r"UniqueViolation|duplicate key|already exists", re.I),
        "This job description already exists. Open the existing one instead of re-uploading.",
    ),
    (
        re.compile(r"duplicate", re.I),
        "This document appears to be a duplicate of one already uploaded.",
    ),
    (
        re.compile(r"OperationalError|could not translate host name|could not connect|connection refused", re.I),
        "We couldn't reach a required service. This is on our side — please retry shortly.",
    ),
    (
        re.compile(r"(corrupt|cannot read|unsupported|not a valid|BadZipFile|damaged)", re.I),
        "This file couldn't be read. Please check it opens correctly and re-upload it.",
    ),
    (
        re.compile(r"(no text|empty|blank|insufficient text)", re.I),
        "No readable text was found in this document. If it's a scanned image, upload a text-based PDF or DOCX.",
    ),
    (
        re.compile(r"JSON|validation error|ValidationError|pydantic", re.I),
        "The AI returned a response we couldn't read. Retrying usually resolves this.",
    ),
]


def to_user_message(raw_error: str | None) -> str | None:
    """
    None in, None out — a task that hasn't failed has no message to show,
    and must not be given one.
    """
    if not raw_error:
        return None

    for pattern, message in _PATTERNS:
        if pattern.search(raw_error):
            return message

    return _FALLBACK
