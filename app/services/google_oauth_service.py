from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.core.oauth_state import sign_state, verify_state
from app.models.oauth import UserOAuthToken
from app.repositories.config_repository import ConfigRepository
from app.repositories.oauth_token_repository import OAuthTokenRepository

PROVIDER_GOOGLE = "GOOGLE"
_SCOPES = "https://www.googleapis.com/auth/calendar.events"
_ENCRYPTION_PURPOSE = "OAUTH_TOKEN"
_DEFAULT_REFRESH_BUFFER_SECONDS = 300
_REFRESH_BUFFER_CONFIG_KEY = "OAUTH_TOKEN_REFRESH_BUFFER_SECONDS"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class GoogleOAuthService:
    """
    M12 - Google Meet calendar integration. Mirrors MicrosoftOAuthService's
    shape exactly (same encrypted-token storage, same refresh-buffer
    config, same fallback-to-existing-refresh-token logic), differing only
    where Google's actual API genuinely differs:

    - No tenant concept - Google's endpoints are global, not per-tenant.
    - access_type=offline + prompt=consent on the authorize URL are both
      required to reliably get a refresh_token back at all: without
      access_type=offline Google only ever issues a short-lived access
      token; without prompt=consent, a user who has already consented
      once won't be re-prompted and won't get a *new* refresh_token on a
      later connect (Google issues one on first consent only, by
      default) - both matter, neither is optional here.
    - Google does NOT reliably return a new refresh_token on every
      refresh call the way Microsoft does - the existing
      setdefault-based fallback (inherited from the same pattern written
      for Microsoft) already handles "omitted" correctly, so it's reused
      as-is rather than rewritten "for Google."
    """

    def __init__(
        self,
        oauth_token_repo: OAuthTokenRepository,
        config_repo: ConfigRepository,
        encryption_service: EncryptionService,
        http_client=httpx,
    ):
        self.oauth_token_repo = oauth_token_repo
        self.config_repo = config_repo
        self.encryption_service = encryption_service
        self.http_client = http_client

    def build_authorize_url(self, user_id: str) -> str:
        state = sign_state(user_id, PROVIDER_GOOGLE)
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    def is_connected(self, user_id: str) -> bool:
        return self.oauth_token_repo.get_by_user_and_provider(user_id, PROVIDER_GOOGLE) is not None

    def handle_callback(self, *, code: str, state: str) -> str:
        """
        Verifies `state` was signed for GOOGLE specifically (raises
        ValueError otherwise - see app.core.oauth_state's provider-binding
        for why a state signed for a different provider must never
        verify here), exchanges `code` for tokens, encrypts and upserts
        them. Returns the user_id the tokens were stored for.
        """
        user_id = verify_state(state, PROVIDER_GOOGLE)

        response = self.http_client.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.google_redirect_uri,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        self._store_tokens(user_id, response.json())
        return user_id

    def get_valid_access_token(self, user_id: str) -> str | None:
        """
        Returns a usable access token, refreshing first if it's within
        the configured buffer of expiry (or already past it). Returns None
        if the user has never connected - callers (GoogleCalendarService)
        treat that as "fall back to manual link", not an error. Raises
        httpx.HTTPError if a refresh is needed and Google's token endpoint
        fails - callers are responsible for treating that as a fail-safe
        "no calendar integration this time", not a hard failure.
        """
        token_row = self.oauth_token_repo.get_by_user_and_provider(user_id, PROVIDER_GOOGLE)
        if token_row is None:
            return None

        now = datetime.now(timezone.utc)
        if (token_row.token_expires_at - now).total_seconds() > self._refresh_buffer_seconds():
            return self.encryption_service.decrypt(token_row.access_token_encrypted, token_row.encryption_key_id)

        refresh_token = self.encryption_service.decrypt(
            token_row.refresh_token_encrypted, token_row.encryption_key_id,
        )
        response = self.http_client.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        token_data = response.json()
        # Google does not reliably return a new refresh_token on refresh -
        # always store whatever comes back; fall back to the token just
        # used when the response omits one (the common case for Google,
        # not just a defensive edge case the way it is for Microsoft).
        token_data.setdefault("refresh_token", refresh_token)

        self._store_tokens(user_id, token_data)
        return token_data["access_token"]

    def _refresh_buffer_seconds(self) -> int:
        raw = self.config_repo.get_configs_by_keys([_REFRESH_BUFFER_CONFIG_KEY]).get(_REFRESH_BUFFER_CONFIG_KEY)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_REFRESH_BUFFER_SECONDS

    def _store_tokens(self, user_id: str, token_data: dict) -> None:
        access_ciphertext, key_id = self.encryption_service.encrypt(token_data["access_token"], _ENCRYPTION_PURPOSE)
        refresh_ciphertext, _ = self.encryption_service.encrypt(token_data["refresh_token"], _ENCRYPTION_PURPOSE)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600)))

        token = UserOAuthToken(
            user_id=user_id,
            provider=PROVIDER_GOOGLE,
            access_token_encrypted=access_ciphertext,
            refresh_token_encrypted=refresh_ciphertext,
            encryption_key_id=key_id,
            token_expires_at=expires_at,
            scopes=token_data.get("scope", _SCOPES),
        )
        self.oauth_token_repo.upsert(token)
        self.oauth_token_repo.commit()
