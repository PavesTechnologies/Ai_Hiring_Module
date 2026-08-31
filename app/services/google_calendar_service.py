import logging
import uuid
from datetime import datetime

import httpx

from app.services.google_oauth_service import GoogleOAuthService

logger = logging.getLogger(__name__)

_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# Without sendUpdates, Google adds attendees to the event's data but never
# actually emails them or puts the event on their own calendar - "all"
# notifies every attendee (not just ones outside the organizer's domain,
# unlike "externalOnly"), which matters here since interviewers can be
# either internal staff or external candidates/panelists.
_SEND_UPDATES = "all"


class GoogleCalendarService:
    """
    M12 - creates/updates/deletes the Google Calendar event (with a Meet
    link) behind a MEET-platform interview. Same method signatures as
    MicrosoftCalendarService by design - that's what lets
    InterviewScheduleService dispatch by a plain {platform: service}
    lookup instead of branching on which provider it's talking to - but
    the request/response shapes genuinely differ from Microsoft's and are
    NOT shared:

    - Attendees are flat {"email", "displayName"} here, not Microsoft's
      nested {"emailAddress": {...}, "type"}.
    - The Meet link isn't a single top-level field - it's found by
      scanning conferenceData.entryPoints for the entry whose
      entryPointType is "video" and reading its uri.
    - Updates use PATCH (Google Calendar's documented partial-update
      method - PUT/`events.update` requires the full resource and would
      blank out any field not sent, which we don't want here).

    Every method is fail-safe by design, same reasoning as Microsoft's:
    a not-connected user or any failure at all never blocks scheduling -
    logged via logger.exception, not audit_log or dead_letter_queue.
    """

    def __init__(self, oauth_service: GoogleOAuthService, http_client=httpx):
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
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            },
        }

        try:
            response = self.http_client.post(
                _EVENTS_URL, params={"conferenceDataVersion": 1, "sendUpdates": _SEND_UPDATES}, json=body,
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            return self._extract_meet_link(data), data.get("id")
        except Exception:
            logger.exception("Google Calendar event creation failed for user_id=%s", user_id)
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
                f"{_EVENTS_URL}/{external_calendar_event_id}", params={"sendUpdates": _SEND_UPDATES}, json=body,
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "Google Calendar event update failed for user_id=%s event_id=%s",
                user_id, external_calendar_event_id,
            )

    def delete_event(self, user_id: str, external_calendar_event_id: str) -> None:
        access_token = self._get_access_token(user_id, action="delete")
        if access_token is None:
            return

        try:
            response = self.http_client.delete(
                f"{_EVENTS_URL}/{external_calendar_event_id}", params={"sendUpdates": _SEND_UPDATES},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10.0,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "Google Calendar event deletion failed for user_id=%s event_id=%s - "
                "orphaned calendar event may need manual cleanup",
                user_id, external_calendar_event_id,
            )

    @staticmethod
    def _event_body(
        subject: str, start_at: datetime, end_at: datetime, attendees: list[dict], location: str | None,
    ) -> dict:
        body = {
            "summary": subject,
            "start": {"dateTime": start_at.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_at.isoformat(), "timeZone": "UTC"},
            "attendees": [{"email": a["email"], "displayName": a["name"]} for a in attendees],
        }
        if location:
            body["location"] = location
        return body

    @staticmethod
    def _extract_meet_link(event_data: dict) -> str | None:
        entry_points = (event_data.get("conferenceData") or {}).get("entryPoints") or []
        for entry_point in entry_points:
            if entry_point.get("entryPointType") == "video":
                return entry_point.get("uri")
        return None

    def _get_access_token(self, user_id: str, *, action: str) -> str | None:
        try:
            return self.oauth_service.get_valid_access_token(user_id)
        except Exception:
            logger.exception("Google token resolution failed during %s for user_id=%s", action, user_id)
            return None
