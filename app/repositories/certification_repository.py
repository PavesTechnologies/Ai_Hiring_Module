from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.scoring_v2 import Certification, CertificationSkill


class CertificationRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_all(self) -> list[Certification]:
        return list(self.db.execute(select(Certification).order_by(Certification.name)).scalars())

    def get(self, certification_id: UUID) -> Certification | None:
        return self.db.get(Certification, certification_id)

    def find_by_name(self, name: str) -> Certification | None:
        return self.db.execute(
            select(Certification).where(func.lower(Certification.name) == name.lower())
        ).scalar_one_or_none()

    def add(self, certification: Certification) -> Certification:
        self.db.add(certification)
        self.db.flush()
        return certification

    def delete(self, certification: Certification) -> None:
        self.db.delete(certification)
        self.db.flush()

    def add_skill(self, row: CertificationSkill) -> CertificationSkill:
        self.db.add(row)
        self.db.flush()
        self.db.expire(row, ["created_at"])
        return row

    def delete_skill(self, row: CertificationSkill) -> None:
        self.db.delete(row)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
