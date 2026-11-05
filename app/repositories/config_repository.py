from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import PlatformConfig


class ConfigRepository:

    def __init__(self, db: Session):
        self.db = db

    # def get_configs_by_keys(
    #     self,
    #     keys: list[str],
    # ) -> dict[str, str]:

    #     stmt = (
    #         select(PlatformConfig)
    #         .where(PlatformConfig.key.in_(keys))
    #     )

    #     result = self.db.execute(stmt)

    #     configs = result.scalars().all()

    #     return {
    #         config.key: config.value
    #         for config in configs
    #     }
    
    def get_configs_by_keys(
        self,
        keys: list[str],
    ) -> dict[str, str]:

        stmt = (
            select(PlatformConfig)
            .where(PlatformConfig.key.in_(keys))
        )

        result = self.db.execute(stmt)

        configs = result.scalars().all()

        print("Requested Keys:", keys)

        for config in configs:
            print(config.key, config.value)

        return {
            config.key: config.value
            for config in configs
        }

    def update_configs(
        self,
        updates: dict[str, str],
        updated_by: str,
    ) -> dict[str, str]:
        """
        bulk-update existing platform_config rows by key. Only
        updates rows that already exist — this is not an upsert, since every
        key it's used for (weight/threshold defaults) is expected to already
        be seeded.
        """
        stmt = select(PlatformConfig).where(PlatformConfig.key.in_(updates.keys()))
        rows = self.db.execute(stmt).scalars().all()

        for row in rows:
            row.value = updates[row.key]
            row.updated_by = updated_by
            row.updated_at = datetime.now(timezone.utc)

        self.db.flush()

        return {row.key: row.value for row in rows}

    def update_description(self, key: str, description: str, updated_by: str | None) -> tuple[str | None, str] | None:
        """
        A setting's description (its value is untouched): (old, new), or None when the key doesn't exist.
        The caller audits and commits.
        """
        row = self.db.execute(select(PlatformConfig).where(PlatformConfig.key == key)).scalar_one_or_none()
        if row is None:
            return None
        old = row.description
        row.description = description
        row.updated_by = updated_by
        row.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return old, description

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()