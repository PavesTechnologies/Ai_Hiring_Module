"""Related education fields are HR_ADMIN only (over HTTP)."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.routes.education_field_routes import get_education_field_family_service
from app.db.session import get_db
from app.enums.constants import API_PREFIX

_payload = {}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.list_all.return_value = []
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_education_field_family_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


_BODY = {"field_1": "COMPUTER_SCIENCE", "field_2": "ELECTRONICS_ENGINEERING", "role_domain": "SOFTWARE"}


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_only_hr_admin_can_view_or_edit(client, role):
    http, service = client
    assert http.get("/education-fields/relations", headers=_as(role)).status_code == 403
    assert http.post("/education-fields/relations", json=_BODY, headers=_as(role)).status_code == 403
    assert http.delete(f"/education-fields/relations/{uuid4()}", headers=_as(role)).status_code == 403
    service.add.assert_not_called()
    service.remove.assert_not_called()


def test_hr_admin_lists_with_the_editor_options(client):
    http, service = client
    response = http.get("/education-fields/relations", headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert "COMPUTER_APPLICATIONS" in data["fields"] and "OTHER" not in data["fields"]
    assert data["role_domains"] == ["SOFTWARE", "IT_OPERATIONS", "DATA", "BUSINESS", "OTHER"]


def test_hr_admin_removes(client):
    http, service = client
    relation_id = uuid4()
    assert http.delete(f"/education-fields/relations/{relation_id}", headers=_as("HR_ADMIN")).status_code == 200
    assert service.remove.call_args.args[0] == relation_id
    assert service.remove.call_args.kwargs["actor_role"] == "HR_ADMIN"
