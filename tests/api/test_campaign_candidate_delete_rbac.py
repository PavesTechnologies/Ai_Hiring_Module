"""DELETE /campaign-candidates/{id}: HR_ADMIN or RECRUITER at the route (the service checks it's the campaign's recruiter)."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.enums.constants import API_PREFIX

_payload = {}


@pytest.fixture(autouse=True)
def _campaign_access_granted():
    """Made-up ids over a mocked database: the campaign access check (tested on its own in
    tests/api/test_campaign_access_routes.py) is let through here."""
    from tests.support.campaign_access_world import campaign_access_granted
    with campaign_access_granted():
        yield


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_campaign_candidate_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role, user_id=None):
    _payload.clear()
    _payload.update({"user_id": user_id or f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


@pytest.mark.parametrize("role", ["HIRING_MANAGER", "INTERVIEWER"])
def test_other_roles_are_refused_before_the_service(client, role):
    http, service = client
    assert http.delete(f"/campaign-candidates/{uuid4()}", headers=_as(role)).status_code == 403
    service.delete_campaign_candidate.assert_not_called()


@pytest.mark.parametrize("role", ["HR_ADMIN", "RECRUITER"])
def test_hr_admin_and_recruiters_reach_the_service_with_their_roles(client, role):
    http, service = client
    response = http.delete(f"/campaign-candidates/{uuid4()}", headers=_as(role, "user-7"))
    assert response.status_code == 200
    kwargs = service.delete_campaign_candidate.call_args.kwargs
    assert kwargs["actor_id"] == "user-7" and kwargs["actor_roles"] == [role]
