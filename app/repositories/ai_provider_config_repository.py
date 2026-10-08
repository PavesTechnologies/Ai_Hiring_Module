import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai_provider import AIProviderConfig, AIProviderModel


class AIProviderConfigRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_active(self) -> AIProviderConfig | None:
        stmt = select(AIProviderConfig).where(AIProviderConfig.is_active.is_(True))
        return self.db.execute(stmt).scalars().first()

    def get_by_id(self, config_id: uuid.UUID) -> AIProviderConfig | None:
        return self.db.get(AIProviderConfig, config_id)

    def get_by_provider(self, provider: str) -> AIProviderConfig | None:
        stmt = select(AIProviderConfig).where(AIProviderConfig.provider == provider)
        return self.db.execute(stmt).scalars().first()

    def list(self, *, page: int, page_size: int) -> list[AIProviderConfig]:
        # Active first, then oldest-registered first, so the table order is stable.
        stmt = (
            select(AIProviderConfig)
            .order_by(AIProviderConfig.is_active.desc(), AIProviderConfig.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count(self) -> int:
        return self.db.execute(select(func.count()).select_from(AIProviderConfig)).scalar_one()

    def registered_providers(self) -> set[str]:
        return set(self.db.execute(select(AIProviderConfig.provider)).scalars().all())

    def create(self, config: AIProviderConfig) -> AIProviderConfig:
        self.db.add(config)
        self.db.flush()
        self.db.refresh(config)
        return config

    def update(self, config: AIProviderConfig) -> AIProviderConfig:
        self.db.flush()
        self.db.refresh(config)
        return config

    def replace_models(self, config: AIProviderConfig, assignments) -> None:
        """
        Swaps the provider's whole model set. The old rows are deleted and
        flushed before the new ones are inserted - updating in place could
        briefly break the (provider, usage) / (provider, model) unique
        constraints, e.g. when two purposes swap models.
        """
        config.models.clear()
        self.db.flush()
        config.models.extend(
            AIProviderModel(usage=a.usage, model_name=a.model_name, fallback_model_name=a.fallback_model_name)
            for a in assignments
        )
        self.db.flush()

    def set_active(self, config: AIProviderConfig) -> AIProviderConfig:
        """
        Clears the current active row and flushes *before* setting the new
        one, so the partial unique index on is_active is never violated
        mid-update.
        """
        current = self.get_active()
        if current is not None and current.id != config.id:
            current.is_active = False
            self.db.flush()
        config.is_active = True
        self.db.flush()
        self.db.refresh(config)
        return config

    def delete(self, config: AIProviderConfig) -> None:
        self.db.delete(config)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
