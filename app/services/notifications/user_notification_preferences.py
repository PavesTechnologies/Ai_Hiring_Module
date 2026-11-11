from app.models.email import EmailTriggerEvent
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository

"""
Epic 5 Step 3 - is_notification_enabled(): a user's opt-out from an email trigger event.
queue_internal_user_emails (internal_user_emails.py) checks it before queuing each internal
user's email (HR / ops alerts); candidate and interviewer emails have no users.id row to hold
a preference, so they never consult it. Any new internal-user send path should go through
queue_internal_user_emails or check this before queuing.
"""


def is_notification_enabled(db, user_id: str, trigger_event: EmailTriggerEvent) -> bool:
    """
    Default-on: an unlisted (user_id, trigger_event) pair means enabled -
    this only ever returns False when an explicit opt-out row exists.
    """
    preference = UserNotificationPreferenceRepository(db).get_by_user_id_and_trigger_event(user_id, trigger_event)
    return preference is None or preference.is_enabled
