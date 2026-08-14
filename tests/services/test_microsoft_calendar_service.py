"""
M12 Microsoft Teams calendar integration - MicrosoftCalendarService's
create/update/delete event calls. Every method is fail-safe by design
(never raises, never blocks the caller) - these tests exist specifically
to prove that guarantee holds across every failure mode, not just the
happy path.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services.microsoft_calendar_service import MicrosoftCalendarService


def _response(json_body=None, raises=False):
    resp = MagicMock()
    if json_body is not None:
        resp.json.return_value = json_body
    if raises:
        resp.raise_for_status.side_effect = Exception("Graph error")
    return resp


def _make_env(access_token="valid-token", http_client=None):
    oauth_service = MagicMock()
    oauth_service.get_valid_access_token.return_value = access_token
    service = MicrosoftCalendarService(oauth_service, http_client=http_client or MagicMock())
    return service, oauth_service


_START = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
_END = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
_ATTENDEES = [{"name": "Alice", "email": "alice@example.com"}]


# ----------------------------------------------------------------------
# create_event
# ----------------------------------------------------------------------

def test_create_event_returns_join_url_and_event_id_on_success():
    http_client = MagicMock()
    http_client.post.return_value = _response({
        "id": "graph-event-1", "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup/abc"},
    })
    service, oauth_service = _make_env(http_client=http_client)

    meeting_link, event_id = service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    assert meeting_link == "https://teams.microsoft.com/l/meetup/abc"
    assert event_id == "graph-event-1"
    body = http_client.post.call_args.kwargs["json"]
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["attendees"][0]["emailAddress"]["address"] == "alice@example.com"


def test_create_event_returns_none_none_when_user_not_connected():
    service, oauth_service = _make_env(access_token=None)

    meeting_link, event_id = service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)
    oauth_service.get_valid_access_token.assert_called_once_with("user-1")


def test_create_event_returns_none_none_on_graph_http_failure_never_raises():
    http_client = MagicMock()
    http_client.post.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    meeting_link, event_id = service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)


def test_create_event_returns_none_none_when_token_resolution_itself_raises():
    oauth_service = MagicMock()
    oauth_service.get_valid_access_token.side_effect = Exception("refresh failed")
    service = MicrosoftCalendarService(oauth_service, http_client=MagicMock())

    meeting_link, event_id = service.create_event(
        "user-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    assert (meeting_link, event_id) == (None, None)


def test_create_event_includes_location_when_given():
    http_client = MagicMock()
    http_client.post.return_value = _response({"id": "e1", "onlineMeeting": {"joinUrl": "url"}})
    service, _ = _make_env(http_client=http_client)

    service.create_event(
        "user-1", subject="Onsite Round", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location="Building 4, Room 2",
    )

    body = http_client.post.call_args.kwargs["json"]
    assert body["location"]["displayName"] == "Building 4, Room 2"


# ----------------------------------------------------------------------
# update_event
# ----------------------------------------------------------------------

def test_update_event_patches_the_existing_graph_event():
    http_client = MagicMock()
    http_client.patch.return_value = _response()
    service, _ = _make_env(http_client=http_client)

    service.update_event(
        "user-1", "graph-event-1", subject="Technical Interview", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    http_client.patch.assert_called_once()
    assert "graph-event-1" in http_client.patch.call_args.args[0]


def test_update_event_skipped_cleanly_when_not_connected():
    http_client = MagicMock()
    service, oauth_service = _make_env(access_token=None, http_client=http_client)

    service.update_event(
        "user-1", "graph-event-1", subject="x", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )

    http_client.patch.assert_not_called()


def test_update_event_never_raises_on_graph_failure():
    http_client = MagicMock()
    http_client.patch.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    service.update_event(
        "user-1", "graph-event-1", subject="x", start_at=_START, end_at=_END,
        attendees=_ATTENDEES, location=None,
    )  # must not raise


# ----------------------------------------------------------------------
# delete_event
# ----------------------------------------------------------------------

def test_delete_event_calls_graph_delete():
    http_client = MagicMock()
    http_client.delete.return_value = _response()
    service, _ = _make_env(http_client=http_client)

    service.delete_event("user-1", "graph-event-1")

    http_client.delete.assert_called_once()
    assert "graph-event-1" in http_client.delete.call_args.args[0]


def test_delete_event_skipped_cleanly_when_not_connected():
    http_client = MagicMock()
    service, _ = _make_env(access_token=None, http_client=http_client)

    service.delete_event("user-1", "graph-event-1")

    http_client.delete.assert_not_called()


def test_delete_event_never_raises_on_graph_failure():
    http_client = MagicMock()
    http_client.delete.return_value = _response(raises=True)
    service, _ = _make_env(http_client=http_client)

    service.delete_event("user-1", "graph-event-1")  # must not raise
