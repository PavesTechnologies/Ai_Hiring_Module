from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.education import EducationFieldRelation


class EducationFieldRelationRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_all(self) -> list[EducationFieldRelation]:
        return list(self.db.execute(
            select(EducationFieldRelation).order_by(
                EducationFieldRelation.role_domain, EducationFieldRelation.field_a, EducationFieldRelation.field_b)
        ).scalars())

    def for_domain(self, role_domain: str) -> list[EducationFieldRelation]:
        return list(self.db.execute(
            select(EducationFieldRelation).where(EducationFieldRelation.role_domain == role_domain)
        ).scalars())

    def get(self, relation_id: UUID) -> EducationFieldRelation | None:
        return self.db.get(EducationFieldRelation, relation_id)

    def find(self, field_a: str, field_b: str, role_domain: str) -> EducationFieldRelation | None:
        return self.db.execute(select(EducationFieldRelation).where(
            EducationFieldRelation.field_a == field_a, EducationFieldRelation.field_b == field_b,
            EducationFieldRelation.role_domain == role_domain,
        )).scalar_one_or_none()

    def add(self, relation: EducationFieldRelation) -> EducationFieldRelation:
        self.db.add(relation)
        self.db.flush()
        return relation

    def delete(self, relation: EducationFieldRelation) -> None:
        self.db.delete(relation)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
