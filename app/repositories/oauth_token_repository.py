from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oauth import UserOAuthToken


class OAuthTokenRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_user_and_provider(self, user_id: str, provider: str) -> UserOAuthToken | None:
        stmt = select(UserOAuthToken).where(
            UserOAuthToken.user_id == user_id, UserOAuthToken.provider == provider,
        )
        return self.db.execute(stmt).scalars().first()

    def upsert(self, token: UserOAuthToken) -> UserOAuthToken:
        """
        UNIQUE(user_id, provider) is the hard invariant - a reconnect (or a
        token refresh) always resolves to the same row, never a second one.
        Check-then-create/update, not a SAVEPOINT+IntegrityError pattern:
        there's no concurrent-writer race to guard against here the way
        transition()'s FOR UPDATE lock exists for interview_schedules - a
        given user connecting/refreshing their own token isn't something
        two requests race on in practice, and a lost-update here just means
        the second write wins, which is harmless for a token refresh.
        """
        existing = self.get_by_user_and_provider(token.user_id, token.provider)
        if existing is None:
            self.db.add(token)
            self.db.flush()
            self.db.refresh(token)
            return token

        existing.access_token_encrypted = token.access_token_encrypted
        existing.refresh_token_encrypted = token.refresh_token_encrypted
        existing.encryption_key_id = token.encryption_key_id
        existing.token_expires_at = token.token_expires_at
        existing.scopes = token.scopes
        self.db.flush()
        self.db.refresh(existing)
        return existing

    def update(self, token: UserOAuthToken) -> UserOAuthToken:
        self.db.flush()
        self.db.refresh(token)
        return token

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
