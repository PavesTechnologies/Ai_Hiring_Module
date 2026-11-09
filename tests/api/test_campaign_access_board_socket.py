"""Review item 10: the board websocket runs the same campaign access check as the board route."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.websocket import router as ws_router
from tests.support.campaign_access_world import access_world


def _user(user_id, *roles):
    return SimpleNamespace(user_id=user_id, roles=list(roles), authorised_roles=(), email=None, claims={})


@pytest.mark.parametrize("user_id, role, allowed", [
    ("hm-1", "HIRING_MANAGER", True), ("hm-2", "HIRING_MANAGER", False),
    ("rec-1", "RECRUITER", True), ("rec-c", "RECRUITER", True), ("rec-2", "RECRUITER", False),
    ("hr-1", "HR_ADMIN", True),
])
def test_who_may_watch_a_campaign_board(user_id, role, allowed):
    from app.middleware.rbac import TokenUser
    with access_world() as world, patch("app.db.session.SessionLocal", MagicMock()):
        user = TokenUser(user_id=user_id, email=None, roles=[role])
        assert ws_router._may_watch_campaign(user, world.campaign_a.id) is allowed


def test_a_super_admin_label_the_socket_doesnt_admit_grants_nothing():
    # The socket admits HR_ADMIN, RECRUITER, HIRING_MANAGER - SUPER_ADMIN alongside RECRUITER is judged as a recruiter.
    from app.middleware.rbac import TokenUser
    with access_world() as world, patch("app.db.session.SessionLocal", MagicMock()):
        user = TokenUser(user_id="rec-2", email=None, roles=["SUPER_ADMIN", "RECRUITER"])
        assert ws_router._may_watch_campaign(user, world.campaign_a.id) is False


def test_the_socket_is_closed_before_subscribing_when_access_is_refused():
    from app.main import app
    payload = {"user_id": "hm-2", "email": "hm-2@example.invalid", "roles": ["HIRING_MANAGER"]}
    subscribe = MagicMock()
    with patch("app.websocket.auth.decode_token", lambda token: payload), \
            patch.object(ws_router, "_may_watch_campaign", return_value=False), \
            patch.object(ws_router.redis_subscriber, "subscribe", subscribe):
        from app.enums.constants import API_PREFIX
        client = TestClient(app)
        path = f"{API_PREFIX}/ws/campaign-candidates/campaign/{uuid4()}/board?token=t"
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path) as ws:
                ws.receive_text()
        assert exc.value.code == 1008 and "access to this campaign" in (exc.value.reason or "")
        subscribe.assert_not_called()


def test_the_unknown_skill_candidate_list_shows_only_visible_campaigns():
    from app.api.routes import skill_routes
    with access_world() as world:
        mine = (SimpleNamespace(id=uuid4(), resume_id=world.resume_a, raw_extracted_text="x", confidence=1.0,
                                match_tier="EXACT", created_at=datetime.now(timezone.utc)), SimpleNamespace(id=uuid4()), None)
        theirs = (SimpleNamespace(id=uuid4(), resume_id=world.resume_b, raw_extracted_text="x", confidence=1.0,
                                  match_tier="EXACT", created_at=datetime.now(timezone.utc)), SimpleNamespace(id=uuid4()), None)
        service = MagicMock()
        service.list_candidates_for_unknown_skill.return_value = [mine, theirs]
        service.decrypt_candidate_name.return_value = "[name]"
        user = SimpleNamespace(user_id="rec-1", roles=["RECRUITER"], authorised_roles=("RECRUITER",))
        response = skill_routes.list_candidates_for_unknown_skill(uuid4(), service=service, user=user,
                                                                   access_db=MagicMock())
        assert [item.resume_id for item in response.data] == [world.resume_a]
        hr = SimpleNamespace(user_id="hr-1", roles=["HR_ADMIN"], authorised_roles=("HR_ADMIN",))
        response = skill_routes.list_candidates_for_unknown_skill(uuid4(), service=service, user=hr,
                                                                   access_db=MagicMock())
        assert len(response.data) == 2
