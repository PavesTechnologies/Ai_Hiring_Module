from fastapi import APIRouter, Depends

from app.dependencies.user_notification_preference import get_user_notification_preference_service
from app.middleware.rbac import get_current_user_id
from app.models.email import EmailTriggerEvent
from app.schemas.response import APIResponse
from app.schemas.user_notification_preference_schema import (
    NotificationPreferenceResponse,
    SetNotificationPreferenceRequest,
)
from app.services.user_notification_preference_service import UserNotificationPreferenceService

router = APIRouter(tags=["User Notification Preferences"])


@router.get(
    "/users/me/notification-preferences",
    response_model=APIResponse[list[NotificationPreferenceResponse]],
    summary="Get My Notification Preferences",
    description=(
        "Epic 5 Step 3 - lists every known trigger event with its "
        "effective enabled state (default-on: an event with no explicit "
        "row is enabled). Any authenticated user manages only their own "
        "preferences. Ready-but-unconsumed today - no send path checks "
        "these yet (see is_notification_enabled()'s own docstring)."
    ),
)
def get_my_notification_preferences(
    user_id: str = Depends(get_current_user_id),
    service: UserNotificationPreferenceService = Depends(get_user_notification_preference_service),
):
    return APIResponse.ok(data=service.get_preferences(user_id))


@router.put(
    "/users/me/notification-preferences/{trigger_event}",
    response_model=APIResponse[NotificationPreferenceResponse],
    summary="Set One Notification Preference",
    description="Enable or disable one trigger_event for the caller. Upserts - a first-ever call for a given event creates its row.",
)
def set_my_notification_preference(
    trigger_event: EmailTriggerEvent,
    request: SetNotificationPreferenceRequest,
    user_id: str = Depends(get_current_user_id),
    service: UserNotificationPreferenceService = Depends(get_user_notification_preference_service),
):
    return APIResponse.ok(data=service.set_preference(user_id, trigger_event, request.is_enabled))
