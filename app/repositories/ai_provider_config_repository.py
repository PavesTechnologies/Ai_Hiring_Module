from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_provider import AIProviderConfig


class AIProviderConfigRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_active(self) -> AIProviderConfig | None:
        stmt = select(AIProviderConfig).where(AIProviderConfig.is_active.is_(True))
        return self.db.execute(stmt).scalars().first()

    def upsert_active(self, config: AIProviderConfig) -> AIProviderConfig:
        """
        Single active row, updated in place - the partial unique index on
        is_active is the hard invariant, this just never tries to break it.
        """
        existing = self.get_active()
        if existing is None:
            self.db.add(config)
            self.db.flush()
            self.db.refresh(config)
            return config

        existing.provider = config.provider
        existing.model_name = config.model_name
        existing.api_key_encrypted = config.api_key_encrypted
        existing.encryption_key_id = config.encryption_key_id
        existing.api_key_last4 = config.api_key_last4
        existing.is_verified = config.is_verified
        existing.verified_at = config.verified_at
        existing.last_error = config.last_error
        existing.updated_by = config.updated_by
        self.db.flush()
        self.db.refresh(existing)
        return existing

    def deactivate(self, config: AIProviderConfig) -> None:
        config.is_active = False
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
