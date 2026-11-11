import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import PlatformConfig


class ConfigRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_configs_by_keys(self, keys: list[str]) -> dict[str, str]:
        rows = self.db.execute(select(PlatformConfig).where(PlatformConfig.key.in_(keys))).scalars().all()
        return {row.key: row.value for row in rows}

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

    def upsert_configs(self, updates: dict[str, str], updated_by: str, descriptions: dict[str, str]) -> None:
        """Set each key's value, creating the row (with its description) when the key was never seeded."""
        now = datetime.now(timezone.utc)
        rows = {row.key: row for row in self.db.execute(
            select(PlatformConfig).where(PlatformConfig.key.in_(updates.keys()))).scalars().all()}
        for key, value in updates.items():
            row = rows.get(key)
            if row is None:
                self.db.add(PlatformConfig(id=uuid.uuid4(), key=key, value=value, description=descriptions.get(key),
                                           updated_by=updated_by, updated_at=now))
            else:
                row.value, row.updated_by, row.updated_at = value, updated_by, now
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()