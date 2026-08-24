from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository
from app.services.user_notification_preference_service import UserNotificationPreferenceService


def get_user_notification_preference_repository(db: Session = Depends(get_db)) -> UserNotificationPreferenceRepository:
    return UserNotificationPreferenceRepository(db)


def get_user_notification_preference_service(
    repo: UserNotificationPreferenceRepository = Depends(get_user_notification_preference_repository),
) -> UserNotificationPreferenceService:
    return UserNotificationPreferenceService(repo)
