from unittest.mock import MagicMock

from app.models.oauth import UserOAuthToken
from app.repositories.oauth_token_repository import OAuthTokenRepository


def _repo():
    db = MagicMock()
    return OAuthTokenRepository(db), db


def _make_token(user_id="user-1", provider="MICROSOFT"):
    return UserOAuthToken(
        user_id=user_id, provider=provider,
        access_token_encrypted=b"access", refresh_token_encrypted=b"refresh",
        token_expires_at=None, scopes=None,
    )


def test_get_by_user_and_provider_returns_none_when_no_row():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.first.return_value = None

    result = repo.get_by_user_and_provider("user-1", "MICROSOFT")

    assert result is None


def test_upsert_adds_a_new_row_when_none_exists():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.first.return_value = None
    token = _make_token()

    result = repo.upsert(token)

    assert result is token
    db.add.assert_called_once_with(token)
    db.flush.assert_called_once()


def test_upsert_updates_the_existing_row_in_place_when_one_exists():
    repo, db = _repo()
    existing = MagicMock(spec=UserOAuthToken)
    db.execute.return_value.scalars.return_value.first.return_value = existing
    new_token = _make_token()
    new_token.access_token_encrypted = b"new-access"
    new_token.refresh_token_encrypted = b"new-refresh"

    result = repo.upsert(new_token)

    assert result is existing
    assert existing.access_token_encrypted == b"new-access"
    assert existing.refresh_token_encrypted == b"new-refresh"
    db.add.assert_not_called()
