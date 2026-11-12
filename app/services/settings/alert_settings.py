"""
Alert recipients on the Settings page (settings phase 1; decision 2026-11-11: every active HR admin,
secondary-role HR admins included, each able to opt out per alert).

Lists each internal alert with its recipients and their opt-out state, and lets an HR admin switch a
recipient on or off. The audit records ids and the flag only.
"""
from __future__ import annotations

from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.email import EmailTriggerEvent
from app.models.identity import UserRole
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository
from app.repositories.user_repository import UserRepository

_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")

# The internal alerts that are sent today, and the settings that tune them (General tab).
ALERTS = (
    {"trigger_event": EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT, "label": "Emails stuck in sending",
     "description": "Emails the mail service hasn't confirmed for more than 30 minutes.",
     "settings": ["EMAIL_SEND_STUCK_ALERT_REPEAT_HOURS"]},
    {"trigger_event": EmailTriggerEvent.EMBEDDING_HEALTH_ALERT, "label": "Embedding failures",
     "description": "A campaign's screening candidates can't be embedded for semantic scoring.",
     "settings": ["EMBEDDING_FAILURE_ALERT_THRESHOLD", "EMBEDDING_FAILURE_ALERT_REPEAT_HOURS"]},
)
_BY_TRIGGER = {a["trigger_event"].value: a for a in ALERTS}


class AlertSettingsService:
    def __init__(self, db, audit_service):
        self.db = db
        self.users = UserRepository(db)
        self.preferences = UserNotificationPreferenceRepository(db)
        self.audit_service = audit_service

    def _hr_admins(self):
        return sorted(self.users.get_active_by_role(UserRole.HR_ADMIN),
                      key=lambda u: ((u.full_name or "").lower(), u.email or ""))

    def _enabled(self, user_id: str, trigger_event) -> bool:
        pref = self.preferences.get_by_user_id_and_trigger_event(user_id, trigger_event)
        return pref is None or pref.is_enabled

    def list(self) -> list[dict]:
        admins = self._hr_admins()
        result = []
        for alert in ALERTS:
            recipients = []
            for user in admins:
                pref = self.preferences.get_by_user_id_and_trigger_event(user.id, alert["trigger_event"])
                recipients.append({
                    "user_id": user.id, "name": user.full_name, "email": user.email,
                    "has_email": bool((user.email or "").strip()),
                    "enabled": pref is None or pref.is_enabled,
                })
            result.append({"trigger_event": alert["trigger_event"].value, "label": alert["label"],
                           "description": alert["description"], "settings": alert["settings"],
                           "recipients": recipients,
                           "receiving": sum(1 for r in recipients if r["enabled"] and r["has_email"])})
        return result

    def set_recipient(self, trigger_event: str, user_id: str, enabled: bool, *, actor_id: str) -> list[dict]:
        alert = _BY_TRIGGER.get(trigger_event)
        if alert is None:
            raise CampaignException("Unknown alert.", 404)
        if not isinstance(enabled, bool):
            raise CampaignException('Send {"enabled": true} or {"enabled": false}.', 422)
        admins = self._hr_admins()
        if user_id not in {u.id for u in admins}:
            raise CampaignException("Only active HR admins receive these alerts.", 422)
        if enabled is False:
            # Never leave an alert with nobody to receive it (approved 2026-11-12).
            receiving = [u for u in admins if (u.email or "").strip() and self._enabled(u.id, alert["trigger_event"])]
            if [u.id for u in receiving] == [user_id]:
                raise CampaignException(
                    f"{receiving[0].full_name or receiving[0].email} is the only HR admin receiving "
                    f"\"{alert['label']}\". Switch another HR admin on first, so the alert still reaches someone.",
                    422, data={"code": "LAST_ALERT_RECIPIENT"})
        try:
            pref = self.preferences.get_by_user_id_and_trigger_event(user_id, alert["trigger_event"])
            before = pref is None or pref.is_enabled
            if before != enabled:
                self.preferences.upsert(user_id, alert["trigger_event"], enabled)
                self.audit_service.log(
                    actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
                    action_type=ActionType.PLATFORM_CONFIG_UPDATED.value,
                    entity_type=EntityType.PLATFORM_CONFIG.value, entity_id=_AUDIT_ENTITY_ID, campaign_id=None,
                    details={"title": "Alert recipient changed", "trigger_event": trigger_event,
                             "recipient_user_id": user_id, "enabled": {"before": before, "after": enabled}},
                )
                self.preferences.commit()
        except Exception:
            self.preferences.rollback()
            raise
        return self.list()
