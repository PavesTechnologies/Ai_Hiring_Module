from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.core.oauth_state import sign_state, verify_state
from app.models.oauth import UserOAuthToken
from app.repositories.config_repository import ConfigRepository
from app.repositories.oauth_token_repository import OAuthTokenRepository

PROVIDER_MICROSOFT = "MICROSOFT"
_SCOPES = "Calendars.ReadWrite OnlineMeetings.ReadWrite offline_access User.Read"
_ENCRYPTION_PURPOSE = "OAUTH_TOKEN"
_DEFAULT_REFRESH_BUFFER_SECONDS = 300
_REFRESH_BUFFER_CONFIG_KEY = "OAUTH_TOKEN_REFRESH_BUFFER_SECONDS"


class MicrosoftOAuthService:
    """
    M12 - the Microsoft delegated-OAuth connect/callback/status flow, plus
    get_valid_access_token (Step 3's refresh helper). Calendar-event
    operations live in MicrosoftCalendarService, which depends on this
    class only for get_valid_access_token - "manage the connection" and
    "use the connection to do calendar things" are different concerns,
    same split as StageTransitionService vs CampaignCandidateService.
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
        state = sign_state(user_id, PROVIDER_MICROSOFT)
        params = {
            "client_id": settings.microsoft_client_id,
            "response_type": "code",
            "redirect_uri": settings.microsoft_redirect_uri,
            "response_mode": "query",
            "scope": _SCOPES,
            "state": state,
        }
        return (
            f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}"
            f"/oauth2/v2.0/authorize?{urlencode(params)}"
        )

    def is_connected(self, user_id: str) -> bool:
        return self.oauth_token_repo.get_by_user_and_provider(user_id, PROVIDER_MICROSOFT) is not None

    def handle_callback(self, *, code: str, state: str) -> str:
        """
        Verifies `state` (raises ValueError on forged/expired/malformed -
        callers must treat that as a rejected callback, not proceed
        anyway), exchanges `code` for tokens, encrypts and upserts them.
        Returns the user_id the tokens were stored for.
        """
        user_id = verify_state(state, PROVIDER_MICROSOFT)

        response = self.http_client.post(
            f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.microsoft_redirect_uri,
                "scope": _SCOPES,
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
        if the user has never connected - callers (MicrosoftCalendarService)
        treat that as "fall back to manual link", not an error. Raises
        httpx.HTTPError if a refresh is needed and Microsoft's token
        endpoint fails - callers are responsible for treating that as a
        fail-safe "no calendar integration this time", not a hard failure.
        """
        token_row = self.oauth_token_repo.get_by_user_and_provider(user_id, PROVIDER_MICROSOFT)
        if token_row is None:
            return None

        now = datetime.now(timezone.utc)
        if (token_row.token_expires_at - now).total_seconds() > self._refresh_buffer_seconds():
            return self.encryption_service.decrypt(token_row.access_token_encrypted, token_row.encryption_key_id)

        refresh_token = self.encryption_service.decrypt(
            token_row.refresh_token_encrypted, token_row.encryption_key_id,
        )
        response = self.http_client.post(
            f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": _SCOPES,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        token_data = response.json()
        # Microsoft's v2.0 endpoint rotates the refresh token on every
        # refresh - always store whatever comes back; fall back to the
        # token just used only if the response omits one (undocumented,
        # cheap to guard against).
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
            provider=PROVIDER_MICROSOFT,
            access_token_encrypted=access_ciphertext,
            refresh_token_encrypted=refresh_ciphertext,
            encryption_key_id=key_id,
            token_expires_at=expires_at,
            scopes=token_data.get("scope", _SCOPES),
        )
        self.oauth_token_repo.upsert(token)
        self.oauth_token_repo.commit()
