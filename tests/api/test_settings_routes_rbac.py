"""The Settings API is HR_ADMIN only, and reports each invalid value (over HTTP)."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes.settings_routes import get_platform_settings_service
from app.db.session import get_db
from app.enums.constants import API_PREFIX
from app.services.settings.platform_settings import InvalidSetting

_payload = {}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.list.return_value = []
    service.preview.return_value = {"changes": [], "warnings": []}
    service.save.return_value = {"changed": ["SCREENING_SLA_HOURS"], "settings": []}
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_platform_settings_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


_BODY = {"values": {"SCREENING_SLA_HOURS": 24}}


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_only_hr_admin_can_view_preview_or_save(client, role):
    http, service = client
    assert http.get("/settings/platform", headers=_as(role)).status_code == 403
    assert http.post("/settings/platform/preview", json=_BODY, headers=_as(role)).status_code == 403
    assert http.put("/settings/platform", json=_BODY, headers=_as(role)).status_code == 403
    service.save.assert_not_called()


def test_hr_admin_saves_as_themselves(client):
    http, service = client
    response = http.put("/settings/platform", json=_BODY, headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    assert service.save.call_args.kwargs["actor_id"] == "hr_admin-1"
    assert service.save.call_args.args[0] == {"SCREENING_SLA_HOURS": 24}


def test_invalid_values_come_back_per_setting(client):
    http, service = client
    service.save.side_effect = InvalidSetting({"MAX_FILES_PER_ZIP": "Enter a value from 1 to 1000."})
    response = http.put("/settings/platform", json={"values": {"MAX_FILES_PER_ZIP": 0}}, headers=_as("HR_ADMIN"))
    assert response.status_code == 422
    assert response.json()["data"]["problems"] == {"MAX_FILES_PER_ZIP": "Enter a value from 1 to 1000."}


def test_a_body_without_values_is_refused(client):
    http, service = client
    assert http.put("/settings/platform", json={"SCREENING_SLA_HOURS": 24}, headers=_as("HR_ADMIN")).status_code == 422
    service.save.assert_not_called()
