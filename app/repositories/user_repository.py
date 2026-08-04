from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity import User, UserRole


class UserRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_active_by_role(self, role: UserRole) -> list[User]:
        """
        Every active user with this role - e.g. every HR_ADMIN to notify
        for a platform-wide alert that isn't tied to any one candidate
        (see embedding_health_tasks.py). User.email is stored in plaintext
        (unlike Candidate's encrypted PII columns), so no decryption step
        is needed before using it as an email recipient.
        """
        stmt = select(User).where(User.role == role, User.is_active.is_(True))
        return list(self.db.execute(stmt).scalars().all())
