import uuid

from sqlalchemy.orm import Session

from app.models.config import EncryptionKey, KeyStatus


class EncryptionKeyRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_purpose(self, purpose: str) -> EncryptionKey | None:
        return (
            self.db.query(EncryptionKey)
            .filter(
                EncryptionKey.purpose == purpose,
                EncryptionKey.key_status == KeyStatus.ACTIVE,
            )
            .first()
        )

    def get_rotating_by_purpose(self, purpose: str) -> EncryptionKey | None:
        return (
            self.db.query(EncryptionKey)
            .filter(
                EncryptionKey.purpose == purpose,
                EncryptionKey.key_status == KeyStatus.ROTATING,
            )
            .first()
        )

    def get_by_id(self, key_id: uuid.UUID) -> EncryptionKey | None:
        return (
            self.db.query(EncryptionKey)
            .filter(EncryptionKey.id == key_id)
            .first()
        )
