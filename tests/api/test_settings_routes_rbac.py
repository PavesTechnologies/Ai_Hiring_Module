"""The Settings API is HR_ADMIN only, and reports each invalid value (over HTTP)."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes.settings_routes import (
    get_alert_settings_service, get_email_template_settings_service, get_platform_settings_service,
    get_reason_code_settings_service, get_transition_settings_service,
)
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
    service.set_recipient.return_value = []
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_platform_settings_service] = lambda: service
        app.dependency_overrides[get_alert_settings_service] = lambda: service
        app.dependency_overrides[get_reason_code_settings_service] = lambda: service
        app.dependency_overrides[get_email_template_settings_service] = lambda: service
        app.dependency_overrides[get_transition_settings_service] = lambda: service
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


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_alerts_and_schedules_are_hr_admin_only(client, role):
    http, service = client
    assert http.get("/settings/alerts", headers=_as(role)).status_code == 403
    assert http.put("/settings/alerts/EMAIL_SEND_STUCK_ALERT/recipients/u1", json={"enabled": False},
                    headers=_as(role)).status_code == 403
    assert http.get("/settings/schedules", headers=_as(role)).status_code == 403
    service.set_recipient.assert_not_called()


def test_hr_admin_switches_a_recipient_as_themselves(client):
    http, service = client
    response = http.put("/settings/alerts/EMAIL_SEND_STUCK_ALERT/recipients/u1", json={"enabled": False},
                        headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    assert service.set_recipient.call_args.args == ("EMAIL_SEND_STUCK_ALERT", "u1", False)
    assert service.set_recipient.call_args.kwargs["actor_id"] == "hr_admin-1"


def test_hr_admin_sees_the_schedules(client):
    http, _ = client
    response = http.get("/settings/schedules", headers=_as("HR_ADMIN"))
    assert response.status_code == 200 and response.json()["data"]["jobs"]


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_reason_code_settings_are_hr_admin_only(client, role):
    http, service = client
    assert http.get("/settings/reason-codes", headers=_as(role)).status_code == 403
    assert http.post("/settings/reason-codes", json={"code": "NEW_CODE", "label": "New"},
                     headers=_as(role)).status_code == 403
    assert http.patch("/settings/reason-codes/ROLE_FIT", json={"is_active": False}, headers=_as(role)).status_code == 403
    service.create.assert_not_called()
    service.update.assert_not_called()


def test_hr_admin_changes_a_reason_code_as_themselves(client):
    http, service = client
    service.update.return_value = []
    response = http.patch("/settings/reason-codes/ROLE_FIT", json={"is_active": False}, headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    assert service.update.call_args.args == ("ROLE_FIT", {"is_active": False})
    assert service.update.call_args.kwargs["actor_id"] == "hr_admin-1"


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_email_template_settings_are_hr_admin_only(client, role):
    http, service = client
    body = {"name": "n", "subject": "s", "body": "b"}
    assert http.get("/settings/email-templates", headers=_as(role)).status_code == 403
    assert http.get("/settings/email-templates/CANDIDATE_REJECTED", headers=_as(role)).status_code == 403
    assert http.post("/settings/email-templates/CANDIDATE_REJECTED/preview", json=body,
                     headers=_as(role)).status_code == 403
    assert http.put("/settings/email-templates/CANDIDATE_REJECTED", json=body, headers=_as(role)).status_code == 403
    assert http.post("/settings/email-templates/CANDIDATE_REJECTED/versions/x/restore",
                     headers=_as(role)).status_code == 403
    service.save.assert_not_called()
    service.restore.assert_not_called()


def test_hr_admin_saves_a_template_as_themselves(client):
    http, service = client
    service.save.return_value = {"changed": True}
    body = {"name": "n", "subject": "s", "body": "b"}
    response = http.put("/settings/email-templates/CANDIDATE_REJECTED", json=body, headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    assert service.save.call_args.args == ("CANDIDATE_REJECTED", body)
    assert service.save.call_args.kwargs["actor_id"] == "hr_admin-1"


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_transition_settings_are_hr_admin_only(client, role):
    http, service = client
    body = {"changes": [{"op": "remove", "id": "x"}]}
    assert http.get("/settings/transitions", headers=_as(role)).status_code == 403
    assert http.post("/settings/transitions/preview", json=body, headers=_as(role)).status_code == 403
    assert http.put("/settings/transitions", json=body, headers=_as(role)).status_code == 403
    service.save.assert_not_called()


def test_hr_admin_saves_transitions_as_themselves(client):
    http, service = client
    service.save.return_value = {"changed": 1}
    body = {"changes": [{"op": "update", "id": "x", "requires_reason": True}]}
    assert http.put("/settings/transitions", json=body, headers=_as("HR_ADMIN")).status_code == 200
    assert service.save.call_args.args == (body["changes"],)
    assert service.save.call_args.kwargs["actor_id"] == "hr_admin-1"
    assert http.put("/settings/transitions", json={"op": "remove"}, headers=_as("HR_ADMIN")).status_code == 422


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_golden_runs_are_hr_admin_only(client, role):
    http, _ = client
    with patch("app.api.routes.settings_routes.GoldenRunService") as service:
        assert http.get("/settings/scoring-versions/v1/golden-runs", headers=_as(role)).status_code == 403
        assert http.get("/settings/scoring-versions/v1/golden-runs/budget", headers=_as(role)).status_code == 403
        assert http.post("/settings/scoring-versions/v1/golden-runs", json={"kind": "ESTIMATE"},
                         headers=_as(role)).status_code == 403
    service.return_value.start.assert_not_called()


def test_hr_admin_starts_a_golden_run_as_themselves(client):
    http, _ = client
    with patch("app.api.routes.settings_routes.GoldenRunService") as service:
        service.return_value.start.return_value = {"id": "r1", "status": "QUEUED"}
        response = http.post("/settings/scoring-versions/v1/golden-runs", json={"kind": "RUN"}, headers=_as("HR_ADMIN"))
    assert response.status_code == 202
    assert service.return_value.start.call_args.args == ("v1", "RUN")
    assert service.return_value.start.call_args.kwargs["actor_id"] == "hr_admin-1"


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_scoring_config_is_hr_admin_only(client, role):
    http, _ = client
    assert http.get("/settings/scoring-config", headers=_as(role)).status_code == 403


def test_scoring_config_gives_defaults_and_ranges_without_the_shared_evidence(client):
    http, _ = client
    with patch("app.services.campaign.scoring_versions.current_version", return_value=None):
        data = http.get("/settings/scoring-config", headers=_as("HR_ADMIN")).json()["data"]
    assert "evidence" not in data["defaults"] and "evidence" not in data["schema"]
    assert data["schema"]["validation"]["experience_tolerance_years"] == {"type": "number", "min": 0, "max": 10}
    assert data["defaults"]["validation"]["equivalent_experience_years"] == 8.0
