import time

import pytest

from app.core import oauth_state
from app.core.config import settings


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(settings, "oauth_state_signing_key", "test-signing-key")


def test_sign_then_verify_round_trips_the_user_id():
    state = oauth_state.sign_state("user-1", "MICROSOFT")

    result = oauth_state.verify_state(state, "MICROSOFT")

    assert result == "user-1"


def test_verify_rejects_a_tampered_payload():
    state = oauth_state.sign_state("user-1", "MICROSOFT")
    payload_b64, signature = state.split(".", 1)
    tampered = f"{payload_b64}x.{signature}"

    with pytest.raises(ValueError):
        oauth_state.verify_state(tampered, "MICROSOFT")


def test_verify_rejects_a_forged_signature():
    state = oauth_state.sign_state("user-1", "MICROSOFT")
    payload_b64, _ = state.split(".", 1)
    forged = f"{payload_b64}.not-the-real-signature"

    with pytest.raises(ValueError):
        oauth_state.verify_state(forged, "MICROSOFT")


def test_verify_rejects_a_malformed_state_with_no_separator():
    with pytest.raises(ValueError):
        oauth_state.verify_state("not-a-valid-state-at-all", "MICROSOFT")


def test_verify_rejects_an_expired_state(monkeypatch):
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 1000)
    state = oauth_state.sign_state("user-1", "MICROSOFT", ttl_seconds=600)
    monkeypatch.setattr(time, "time", real_time)

    with pytest.raises(ValueError, match="expired"):
        oauth_state.verify_state(state, "MICROSOFT")


def test_different_users_produce_different_signed_states():
    assert oauth_state.sign_state("user-1", "MICROSOFT") != oauth_state.sign_state("user-2", "MICROSOFT")


# ----------------------------------------------------------------------
# Provider binding - a state signed for one provider must never verify
# against a different one. This is the fix for a real cross-provider
# replay risk found while building Google Meet integration (the second
# provider): without this, a stolen state value could be replayed against
# either provider's /callback, silently attaching an attacker's calendar
# account to the victim's user_oauth_tokens row.
# ----------------------------------------------------------------------

def test_verify_rejects_a_state_signed_for_a_different_provider():
    state = oauth_state.sign_state("user-1", "MICROSOFT")

    with pytest.raises(ValueError, match="different provider"):
        oauth_state.verify_state(state, "GOOGLE")


def test_same_user_different_provider_still_verifies_correctly_for_each():
    microsoft_state = oauth_state.sign_state("user-1", "MICROSOFT")
    google_state = oauth_state.sign_state("user-1", "GOOGLE")

    assert oauth_state.verify_state(microsoft_state, "MICROSOFT") == "user-1"
    assert oauth_state.verify_state(google_state, "GOOGLE") == "user-1"
    with pytest.raises(ValueError):
        oauth_state.verify_state(microsoft_state, "GOOGLE")
    with pytest.raises(ValueError):
        oauth_state.verify_state(google_state, "MICROSOFT")
