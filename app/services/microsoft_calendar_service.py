import logging
from datetime import datetime

import httpx

from app.services.microsoft_oauth_service import MicrosoftOAuthService

logger = logging.getLogger(__name__)

_GRAPH_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/events"


class MicrosoftCalendarService:
    """
    M12 - creates/updates/deletes the Microsoft Teams calendar event behind
    a TEAMS-platform interview. Every method is deliberately fail-safe: a
    not-connected user or any failure at all (network, expired/revoked
    token even after refresh, a malformed Graph response, a Graph error
    status) never blocks scheduling/reschedule/cancel in our own system -
    it just means no meeting_link/calendar event exists, the same outcome
    as the platform not being TEAMS. That's why the except clauses below
    are deliberately broad (Exception, not just httpx.HTTPError) - the
    contract is "never blocks the caller," not "never blocks the caller
    for the failure modes we happened to anticipate."

    Failures are application-logged, not written to audit_log (reserved
    for business-actor events - scheduling itself, not the infrastructure
    behind it) or dead_letter_queue (FK'd to celery_task_log; this runs
    synchronously inside the HTTP request, not as a retryable Celery task,
    so there's no natural DLQ row for it).
    """

    def __init__(self, oauth_service: MicrosoftOAuthService, http_client=httpx):
        self.oauth_service = oauth_service
        self.http_client = http_client

    def create_event(
        self, user_id: str, *, subject: str, start_at: datetime, end_at: datetime,
        attendees: list[dict], location: str | None,
    ) -> tuple[str | None, str | None]:
        """Returns (meeting_link, external_calendar_event_id) - (None, None) if not connected or on any failure."""
        access_token = self._get_access_token(user_id, action="create")
        if access_token is None:
            return None, None

        body = self._event_body(subject, start_at, end_at, attendees, location)
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"

        try:
            response = self.http_client.post(
                _GRAPH_EVENTS_URL, json=body,
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            return (data.get("onlineMeeting") or {}).get("joinUrl"), data.get("id")
        except Exception:
            logger.exception("Microsoft Graph event creation failed for user_id=%s", user_id)
            return None, None

    def update_event(
        self, user_id: str, external_calendar_event_id: str, *, subject: str, start_at: datetime,
        end_at: datetime, attendees: list[dict], location: str | None,
    ) -> None:
        access_token = self._get_access_token(user_id, action="update")
        if access_token is None:
            return

        body = self._event_body(subject, start_at, end_at, attendees, location)

        try:
            response = self.http_client.patch(
                f"{_GRAPH_EVENTS_URL}/{external_calendar_event_id}", json=body,
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "Microsoft Graph event update failed for user_id=%s event_id=%s",
                user_id, external_calendar_event_id,
            )

    def delete_event(self, user_id: str, external_calendar_event_id: str) -> None:
        access_token = self._get_access_token(user_id, action="delete")
        if access_token is None:
            return

        try:
            response = self.http_client.delete(
                f"{_GRAPH_EVENTS_URL}/{external_calendar_event_id}",
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "Microsoft Graph event deletion failed for user_id=%s event_id=%s - "
                "orphaned calendar event may need manual cleanup",
                user_id, external_calendar_event_id,
            )

    @staticmethod
    def _event_body(
        subject: str, start_at: datetime, end_at: datetime, attendees: list[dict], location: str | None,
    ) -> dict:
        body = {
            "subject": subject,
            "start": {"dateTime": start_at.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_at.isoformat(), "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": a["email"], "name": a["name"]}, "type": "required"}
                for a in attendees
            ],
        }
        if location:
            body["location"] = {"displayName": location}
        return body

    def _get_access_token(self, user_id: str, *, action: str) -> str | None:
        try:
            return self.oauth_service.get_valid_access_token(user_id)
        except Exception:
            logger.exception("Microsoft token resolution failed during %s for user_id=%s", action, user_id)
            return None
