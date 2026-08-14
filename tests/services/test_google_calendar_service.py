"""
M12 Google Meet calendar integration - GoogleCalendarService's
create/update/delete event calls. Mirrors
test_microsoft_calendar_service.py's structure; the Google-specific
extraction logic (Meet link from conferenceData.entryPoints, flat
attendee shape, PATCH for updates) gets its own dedicated assertions
rather than assuming the Microsoft shape applies.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services.google_calendar_service import GoogleCalendarService


def _response(json_body=None, raises=False):
    resp = MagicMock()
    if json_body is not None:
        resp.json.return_value = json_body
    if raises:
        resp.raise_for_status.side_effect = Exception("Google API error")
    return resp


def _make_env(access_token="valid-token", http_client=None):
    oauth_service = MagicMock()
    oauth_service.get_valid_access_token.return_value = access_token
    service = GoogleCalendarService(oauth_service, http_client=http_client or MagicMock())
    return service, oauth_service


_START = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
_END = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
_ATTENDEES = [{"name": "Alice", "email": "alice@example.com"}]

_EVENT_WITH_MEET_LINK = {
    "id": "google-event-1",
    "conferenceData": {
        "entryPoints": [
            {"entryPointType": "sip", "uri": "sip:something"},
            {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
        ],
    },
}


# ----------------------------------------------------------------------
# create_event
# ----------------------------------------------------------------------

def test_create_event_extracts_the_video_entry_point_as_the_meet_link():
    http_client = MagicMock()
    http_client.post.return_value = _response(_EVENT_WITH_MEET_LINK)
    service, _ = _make_env(http_client=http_client)

    meeting_link, event_id = service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    assert meeting_link == "https://meet.google.com/abc-defg-hij"
    assert event_id == "google-event-1"
    body = http_client.post.call_args.kwargs["json"]
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert http_client.post.call_args.kwargs["params"] == {"conferenceDataVersion": 1}


def test_create_event_uses_flat_attendee_shape_not_microsofts_nested_one():
    http_client = MagicMock()
    http_client.post.return_value = _response(_EVENT_WITH_MEET_LINK)
    service, _ = _make_env(http_client=http_client)

    service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    attendee = http_client.post.call_args.kwargs["json"]["attendees"][0]
    assert attendee == {"email": "alice@example.com", "displayName": "Alice"}


def test_create_event_returns_none_when_no_video_entry_point_present():
    http_client = MagicMock()
    http_client.post.return_value = _response({"id": "google-event-1", "conferenceData": {"entryPoints": []}})
    service, _ = _make_env(http_client=http_client)

    meeting_link, event_id = service.create_event(
        "user-1", subject="x", start_at=_START, end_at=_END, attendees=_ATTENDEES, location=None,
    )

    assert meeting_link is None
    assert event_id == "google-event-1"


def test_create_event_returns_none_none_when_user_not_connected():
    service, oauth_service = _make_env(access_token=None)

    meeting_link, event_id = service.create_event(
        "user-1", subject="x", start_at=_START, end_at=_END, attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)


def test_create_event_returns_none_none_on_api_failure_never_raises():
    http_client = MagicMock()
    http_client.post.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    meeting_link, event_id = service.create_event(
        "user-1", subject="x", start_at=_START, end_at=_END, attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)


def test_create_event_returns_none_none_when_token_resolution_itself_raises():
    oauth_service = MagicMock()
    oauth_service.get_valid_access_token.side_effect = Exception("refresh failed")
    service = GoogleCalendarService(oauth_service, http_client=MagicMock())

    meeting_link, event_id = service.create_event(
        "user-1", subject="x", start_at=_START, end_at=_END, attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)


def test_create_event_includes_location_as_a_plain_string():
    http_client = MagicMock()
    http_client.post.return_value = _response(_EVENT_WITH_MEET_LINK)
    service, _ = _make_env(http_client=http_client)

    service.create_event(
        "user-1", subject="Onsite Round", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location="Building 4, Room 2",
    )

    assert http_client.post.call_args.kwargs["json"]["location"] == "Building 4, Room 2"


# ----------------------------------------------------------------------
# update_event - PATCH, not PUT.
# ----------------------------------------------------------------------

def test_update_event_uses_patch_not_put():
    http_client = MagicMock()
    http_client.patch.return_value = _response()
    service, _ = _make_env(http_client=http_client)

    service.update_event(
        "user-1", "google-event-1", subject="x", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    http_client.patch.assert_called_once()
    assert "google-event-1" in http_client.patch.call_args.args[0]
    http_client.put.assert_not_called()


def test_update_event_skipped_cleanly_when_not_connected():
    http_client = MagicMock()
    service, oauth_service = _make_env(access_token=None, http_client=http_client)

    service.update_event(
        "user-1", "google-event-1", subject="x", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    http_client.patch.assert_not_called()


def test_update_event_never_raises_on_api_failure():
    http_client = MagicMock()
    http_client.patch.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    service.update_event(
        "user-1", "google-event-1", subject="x", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )  # must not raise


# ----------------------------------------------------------------------
# delete_event
# ----------------------------------------------------------------------

def test_delete_event_calls_the_api():
    http_client = MagicMock()
    http_client.delete.return_value = _response()
    service, _ = _make_env(http_client=http_client)

    service.delete_event("user-1", "google-event-1")

    http_client.delete.assert_called_once()
    assert "google-event-1" in http_client.delete.call_args.args[0]


def test_delete_event_skipped_cleanly_when_not_connected():
    http_client = MagicMock()
    service, _ = _make_env(access_token=None, http_client=http_client)

    service.delete_event("user-1", "google-event-1")

    http_client.delete.assert_not_called()


def test_delete_event_never_raises_on_api_failure():
    http_client = MagicMock()
    http_client.delete.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    service.delete_event("user-1", "google-event-1")  # must not raise
