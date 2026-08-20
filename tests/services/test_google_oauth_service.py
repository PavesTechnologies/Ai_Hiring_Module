"""
M12 Google Meet calendar integration - GoogleOAuthService's
connect/callback/status flow and the token-refresh helper. Mirrors
test_microsoft_oauth_service.py's structure exactly; the Google-specific
behaviors (access_type=offline + prompt=consent on the authorize URL,
refresh not reliably returning a new refresh_token) get their own
dedicated tests rather than just re-running Microsoft's.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core import oauth_state
from app.core.config import settings
from app.services.google_oauth_service import PROVIDER_GOOGLE, GoogleOAuthService


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(settings, "oauth_state_signing_key", "test-signing-key")


def _response(json_body, raises=False):
    resp = MagicMock()
    resp.json.return_value = json_body
    if raises:
        resp.raise_for_status.side_effect = Exception("Google API error")
    return resp


def _make_env(token_row=None, post_response=None):
    oauth_token_repo = MagicMock()
    oauth_token_repo.get_by_user_and_provider.return_value = token_row
    oauth_token_repo.upsert.side_effect = lambda t: t

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"OAUTH_TOKEN_REFRESH_BUFFER_SECONDS": "300"}

    encryption_service = MagicMock()
    encryption_service.encrypt.side_effect = lambda value, purpose: (f"enc({value})".encode(), uuid4())
    encryption_service.decrypt.side_effect = lambda ciphertext, key_id: ciphertext.decode().removeprefix("enc(").removesuffix(")")

    http_client = MagicMock()
    if post_response is not None:
        http_client.post.return_value = post_response

    service = GoogleOAuthService(oauth_token_repo, config_repo, encryption_service, http_client=http_client)
    return service, oauth_token_repo, config_repo, encryption_service, http_client


def _make_token_row(expires_in_seconds: int, encrypted_access="enc(old-access)", encrypted_refresh="enc(old-refresh)"):
    return SimpleNamespace(
        id=uuid4(),
        user_id="user-1",
        provider=PROVIDER_GOOGLE,
        access_token_encrypted=encrypted_access.encode(),
        refresh_token_encrypted=encrypted_refresh.encode(),
        encryption_key_id=uuid4(),
        token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        scopes="https://www.googleapis.com/auth/calendar.events",
    )


# ----------------------------------------------------------------------
# build_authorize_url / is_connected
# ----------------------------------------------------------------------

def test_build_authorize_url_includes_offline_access_and_forces_consent(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-abc")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:8002/airs/oauth/google/callback")
    service, *_rest = _make_env()

    url = service.build_authorize_url("user-1")

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=client-abc" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=" in url


def test_is_connected_true_when_a_token_row_exists():
    service, repo, *_rest = _make_env(token_row=_make_token_row(3600))

    assert service.is_connected("user-1") is True
    repo.get_by_user_and_provider.assert_called_once_with("user-1", PROVIDER_GOOGLE)


def test_is_connected_false_when_no_token_row():
    service, *_rest = _make_env(token_row=None)

    assert service.is_connected("user-1") is False


# ----------------------------------------------------------------------
# handle_callback - full round-trip with a mocked token endpoint.
# ----------------------------------------------------------------------

def test_handle_callback_exchanges_code_and_stores_encrypted_tokens():
    state = oauth_state.sign_state("user-1", "GOOGLE")
    token_response = _response({
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/calendar.events",
    })
    service, repo, config_repo, encryption_service, http_client = _make_env(post_response=token_response)

    user_id = service.handle_callback(code="auth-code", state=state)

    assert user_id == "user-1"
    call_kwargs = http_client.post.call_args.kwargs
    assert call_kwargs["data"]["code"] == "auth-code"
    assert call_kwargs["data"]["grant_type"] == "authorization_code"
    stored = repo.upsert.call_args.args[0]
    assert stored.provider == PROVIDER_GOOGLE
    repo.commit.assert_called_once()


def test_handle_callback_raises_on_a_state_signed_for_a_different_provider():
    microsoft_state = oauth_state.sign_state("user-1", "MICROSOFT")
    service, *_rest = _make_env()

    with pytest.raises(ValueError):
        service.handle_callback(code="auth-code", state=microsoft_state)


def test_handle_callback_propagates_error_on_token_exchange_failure():
    state = oauth_state.sign_state("user-1", "GOOGLE")
    failing_response = _response({}, raises=True)
    service, *_rest = _make_env(post_response=failing_response)

    with pytest.raises(Exception):
        service.handle_callback(code="auth-code", state=state)


# ----------------------------------------------------------------------
# get_valid_access_token - refresh helper, incl. Google's "usually no new
# refresh_token" behavior.
# ----------------------------------------------------------------------

def test_get_valid_access_token_returns_none_when_not_connected():
    service, *_rest = _make_env(token_row=None)

    assert service.get_valid_access_token("user-1") is None


def test_get_valid_access_token_returns_decrypted_token_without_refreshing_when_far_from_expiry():
    token_row = _make_token_row(3600)
    service, repo, config_repo, encryption_service, http_client = _make_env(token_row=token_row)

    result = service.get_valid_access_token("user-1")

    assert result == "old-access"
    http_client.post.assert_not_called()


def test_get_valid_access_token_refreshes_when_within_the_buffer_window():
    token_row = _make_token_row(100)
    refresh_response = _response({"access_token": "refreshed-access", "expires_in": 3600})  # no refresh_token key
    service, repo, config_repo, encryption_service, http_client = _make_env(
        token_row=token_row, post_response=refresh_response,
    )

    result = service.get_valid_access_token("user-1")

    assert result == "refreshed-access"
    assert http_client.post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    repo.commit.assert_called_once()


def test_refresh_keeps_the_existing_refresh_token_when_google_omits_a_new_one():
    """
    Google's documented default behavior: refresh calls do NOT return a
    new refresh_token. This is the common case for Google, not just a
    defensive fallback the way it is for Microsoft.
    """
    token_row = _make_token_row(100)
    refresh_response = _response({"access_token": "a2", "expires_in": 3600})
    service, repo, *_rest = _make_env(token_row=token_row, post_response=refresh_response)

    service.get_valid_access_token("user-1")

    stored = repo.upsert.call_args.args[0]
    assert stored.refresh_token_encrypted == b"enc(old-refresh)"


def test_refresh_stores_a_new_refresh_token_on_the_rare_occasion_google_returns_one():
    token_row = _make_token_row(100)
    refresh_response = _response({"access_token": "a2", "refresh_token": "rotated", "expires_in": 3600})
    service, repo, *_rest = _make_env(token_row=token_row, post_response=refresh_response)

    service.get_valid_access_token("user-1")

    stored = repo.upsert.call_args.args[0]
    assert stored.refresh_token_encrypted == b"enc(rotated)"
