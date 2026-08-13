from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.saved_views import UserSavedView


class SavedViewRepository:
    """M11-E03-S03 persistence. Every read is scoped by user_id — a saved view
    is private to its owner, so campaign access alone never grants sight of
    someone else's views."""

    def __init__(self, db: Session):
        self.db = db

    def list_for_user(self, user_id: str, campaign_id: UUID) -> list[UserSavedView]:
        return (
            self.db.query(UserSavedView)
            .filter(UserSavedView.user_id == user_id, UserSavedView.campaign_id == campaign_id)
            .order_by(UserSavedView.created_at.asc())
            .all()
        )

    def count_for_user(self, user_id: str, campaign_id: UUID) -> int:
        return (
            self.db.query(func.count(UserSavedView.id))
            .filter(UserSavedView.user_id == user_id, UserSavedView.campaign_id == campaign_id)
            .scalar()
        ) or 0

    def get_owned(self, view_id: UUID, user_id: str) -> UserSavedView | None:
        """Ownership is part of the lookup, not a separate check a caller can forget."""
        return (
            self.db.query(UserSavedView)
            .filter(UserSavedView.id == view_id, UserSavedView.user_id == user_id)
            .first()
        )

    def get_by_name(self, user_id: str, campaign_id: UUID, name: str) -> UserSavedView | None:
        return (
            self.db.query(UserSavedView)
            .filter(
                UserSavedView.user_id == user_id,
                UserSavedView.campaign_id == campaign_id,
                func.lower(UserSavedView.name) == name.lower(),
            )
            .first()
        )

    def add(self, view: UserSavedView) -> UserSavedView:
        self.db.add(view)
        self.db.flush()
        self.db.refresh(view)
        return view

    def delete(self, view: UserSavedView) -> None:
        self.db.delete(view)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
