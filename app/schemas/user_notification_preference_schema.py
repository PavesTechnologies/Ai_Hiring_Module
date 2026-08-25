from pydantic import BaseModel


class NotificationPreferenceResponse(BaseModel):
    """One entry per known trigger_event - always the full set, default-on values filled in for anything not explicitly set."""
    trigger_event: str
    is_enabled: bool


class SetNotificationPreferenceRequest(BaseModel):
    is_enabled: bool
