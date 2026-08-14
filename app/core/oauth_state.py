import base64
import hashlib
import hmac
import json
import time

from app.core.config import settings

_DEFAULT_TTL_SECONDS = 600  # 10 minutes - long enough for a real consent screen, short enough to bound replay risk


def sign_state(user_id: str, provider: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> str:
    """
    Stateless CSRF protection for an OAuth connect/callback round-trip
    (Microsoft, Google, or any future provider). /callback carries no
    Authorization header at all (see JWTMiddleware's public-path bypass
    for each callback route - the provider's redirect is a plain browser
    navigation with no way to attach one), so the state param itself has
    to carry which user initiated the flow.

    `provider` is bound into the signed payload and re-checked by
    verify_state, not just passed around for logging: without this, a
    state value stolen from one provider's flow could be replayed against
    a different provider's /callback (each provider only ever redirects
    to its own registered redirect_uri, but a leaked/intercepted state
    isn't bound by that) - silently attaching an attacker's calendar
    account to the victim's user_oauth_tokens row. This only became a
    real risk once a second provider existed; added when Google Meet
    integration was built, not before.

    Signs {user_id, provider, issued_at, ttl} with a dedicated secret
    (OAUTH_STATE_SIGNING_KEY - never the PII encryption key; signing and
    encryption are different security domains) rather than requiring a
    session store this codebase doesn't otherwise have.
    """
    payload = {"user_id": user_id, "provider": provider, "issued_at": int(time.time()), "ttl": ttl_seconds}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_state(state: str, expected_provider: str) -> str:
    """
    Returns the user_id the state was signed for, only if it was signed
    for `expected_provider`. Raises ValueError on any forged/expired/
    malformed/wrong-provider state - callers must treat that as a
    rejected callback, never fall back to trusting the value anyway.
    """
    try:
        payload_b64, signature = state.split(".", 1)
    except ValueError:
        raise ValueError("Malformed state parameter.")

    if not hmac.compare_digest(signature, _sign(payload_b64)):
        raise ValueError("State parameter signature is invalid.")

    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Malformed state parameter.")

    issued_at = payload.get("issued_at")
    ttl = payload.get("ttl", _DEFAULT_TTL_SECONDS)
    user_id = payload.get("user_id")
    provider = payload.get("provider")
    if not isinstance(issued_at, int) or not isinstance(user_id, str) or not isinstance(provider, str):
        raise ValueError("Malformed state parameter.")

    if provider != expected_provider:
        raise ValueError("State parameter was issued for a different provider.")

    if time.time() > issued_at + ttl:
        raise ValueError("State parameter has expired.")

    return user_id


def _sign(payload_b64: str) -> str:
    key = settings.oauth_state_signing_key.encode("utf-8")
    digest = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
