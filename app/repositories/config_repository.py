from datetime import datetime, timezone

from fastapi.params import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import PlatformConfig
from app.db.session import get_db


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

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()