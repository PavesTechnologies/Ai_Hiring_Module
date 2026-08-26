import base64
import hashlib
import hmac
import json
import time
from uuid import UUID

from app.core.config import settings

# 14 days, not OAuth's 10 minutes - feedback is realistically submitted
# anywhere from same-day to a week-plus after the interview, not within a
# single continuous browser session.
_DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60


def sign_feedback_token(
    interview_schedule_id: UUID, interviewer_id: UUID, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """
    Stateless, no-login access to the interview-feedback form. Interviewers
    have no user account (deliberate - see InterviewInterviewer), so this
    is the entire access-control mechanism for the feedback endpoints -
    same shape as oauth_state.py's signed state (HMAC over a base64url
    JSON payload), but a separate module with its own dedicated secret
    (FEEDBACK_TOKEN_SIGNING_KEY): the two payload shapes and lifetimes are
    different enough that contorting oauth_state.py's OAuth-specific
    fields to also fit this would be messier than a second small module,
    and a leaked key in one domain should never compromise the other.

    Both ids are bound into the signed payload (not just the round) since
    a round can have several interviewers - the token has to identify
    which specific interviewer a given link is for.
    """
    payload = {
        "interview_schedule_id": str(interview_schedule_id),
        "interviewer_id": str(interviewer_id),
        "issued_at": int(time.time()),
        "ttl": ttl_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_feedback_token(token: str) -> tuple[UUID, UUID]:
    """
    Returns (interview_schedule_id, interviewer_id). Raises ValueError on
    any forged/expired/malformed token - callers must treat that as a
    rejected request (404/410), never fall back to trusting the value
    anyway.
    """
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        raise ValueError("Malformed feedback token.")

    if not hmac.compare_digest(signature, _sign(payload_b64)):
        raise ValueError("Feedback token signature is invalid.")

    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Malformed feedback token.")

    issued_at = payload.get("issued_at")
    ttl = payload.get("ttl", _DEFAULT_TTL_SECONDS)
    interview_schedule_id = payload.get("interview_schedule_id")
    interviewer_id = payload.get("interviewer_id")
    if (
        not isinstance(issued_at, int)
        or not isinstance(interview_schedule_id, str)
        or not isinstance(interviewer_id, str)
    ):
        raise ValueError("Malformed feedback token.")

    if time.time() > issued_at + ttl:
        raise ValueError("Feedback token has expired.")

    try:
        return UUID(interview_schedule_id), UUID(interviewer_id)
    except ValueError:
        raise ValueError("Malformed feedback token.")


def _sign(payload_b64: str) -> str:
    key = settings.feedback_token_signing_key.encode("utf-8")
    digest = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
