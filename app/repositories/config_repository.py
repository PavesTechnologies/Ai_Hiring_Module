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