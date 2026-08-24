from app.models.email import EmailTriggerEvent
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository

"""
Epic 5 Step 3 - is_notification_enabled(), built ahead of any real
caller. Of the 6 real EmailTriggerEvent values, the 5 with a live send
path today (see candidate_notification_emails.py,
candidate_rejection_email_service.py) all target a candidate or external
interviewer, neither of which has a users.id row to hold a preference
against - UPLOAD_PERMANENTLY_FAILED is the one trigger actually scoped
for internal users but has no send path of its own yet (D11, unbuilt).
Nothing calls this function today - see docs/known_issues.md's entry
naming this "built ahead of need" shape, matching
SHORTLIST_NOTIFICATION_BATCH_WINDOW_MINUTES. Any future internal-user-
facing send path should check this before queuing.
"""


def is_notification_enabled(db, user_id: str, trigger_event: EmailTriggerEvent) -> bool:
    """
    Default-on: an unlisted (user_id, trigger_event) pair means enabled -
    this only ever returns False when an explicit opt-out row exists.
    """
    preference = UserNotificationPreferenceRepository(db).get_by_user_id_and_trigger_event(user_id, trigger_event)
    return preference is None or preference.is_enabled
