from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.identity import User, UserRole


class UserRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_ids(self, user_ids: list[str]) -> list[User]:
        """Batched lookup - one query per page of a list endpoint, not one per row."""
        if not user_ids:
            return []
        stmt = select(User).where(User.id.in_(user_ids))
        return list(self.db.execute(stmt).scalars().all())

    def get_active_by_role(self, role: UserRole) -> list[User]:
        """
        Every active user with this role - e.g. every HR_ADMIN to notify
        for a platform-wide alert that isn't tied to any one candidate
        (see embedding_health_tasks.py). User.email is stored in plaintext
        (unlike Candidate's encrypted PII columns), so no decryption step
        is needed before using it as an email recipient.
        """
        role_value = getattr(role, "value", role)
        stmt = select(User).where(or_(User.role == role, User.airs_roles.any(role_value)),
                                  User.is_active.is_(True))
        return list(self.db.execute(stmt).scalars().all())
