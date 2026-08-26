from app.models.email import EmailTriggerEvent
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository
from app.schemas.user_notification_preference_schema import NotificationPreferenceResponse


class UserNotificationPreferenceService:
    """
    Epic 5 Step 3 - minimal read/write for a user's own notification
    preferences. Ready-but-unconsumed: no send path checks
    is_notification_enabled() yet (see that function's own docstring and
    docs/known_issues.md) - this class only manages the preference rows
    themselves.
    """

    def __init__(self, user_notification_preference_repo: UserNotificationPreferenceRepository):
        self.user_notification_preference_repo = user_notification_preference_repo

    def get_preferences(self, user_id: str) -> list[NotificationPreferenceResponse]:
        """
        Every real trigger_event, not just rows that exist - default-on
        means an absent row still has to show as enabled, so the caller
        never has to know the default itself.
        """
        explicit = {
            pref.trigger_event: pref.is_enabled
            for pref in self.user_notification_preference_repo.get_all_by_user_id(user_id)
        }
        return [
            NotificationPreferenceResponse(trigger_event=trigger_event.value, is_enabled=explicit.get(trigger_event, True))
            for trigger_event in EmailTriggerEvent
        ]

    def set_preference(
        self, user_id: str, trigger_event: EmailTriggerEvent, is_enabled: bool,
    ) -> NotificationPreferenceResponse:
        preference = self.user_notification_preference_repo.upsert(user_id, trigger_event, is_enabled)
        self.user_notification_preference_repo.commit()
        return NotificationPreferenceResponse(trigger_event=preference.trigger_event.value, is_enabled=preference.is_enabled)
