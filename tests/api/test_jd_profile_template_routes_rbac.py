"""JD profile templates are HR_ADMIN only (over HTTP)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes.jd_profile_template_routes import get_jd_profile_template_service
from app.db.session import get_db
from app.enums.constants import API_PREFIX

_payload = {}

_TEMPLATE = SimpleNamespace(key="FRESHER", label="Fresher", description=None, internship_credit_pct=100,
                            experience_tolerance_years=None, education_requirement="MANDATORY",
                            evidence_sections=["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"],
                            listed_only_can_confirm=True, updated_by=None, updated_at=None)
_BODY = {"label": "Fresher", "internship_credit_pct": 100, "education_requirement": "MANDATORY",
         "evidence_sections": ["EXPERIENCE"], "listed_only_can_confirm": True}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.list_all.return_value = [_TEMPLATE]
    service.update.return_value = (_TEMPLATE, True, 0)
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_jd_profile_template_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_only_hr_admin_can_view_or_edit(client, role):
    http, service = client
    assert http.get("/jd-profile-templates", headers=_as(role)).status_code == 403
    assert http.put("/jd-profile-templates/FRESHER", json=_BODY, headers=_as(role)).status_code == 403
    service.update.assert_not_called()


def test_hr_admin_lists_with_the_editor_options(client):
    http, _ = client
    response = http.get("/jd-profile-templates", headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert [t["key"] for t in data["templates"]] == ["FRESHER"]
    assert data["evidence_section_options"] == ["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"]
    assert data["required_evidence_sections"] == ["EXPERIENCE"]


def test_hr_admin_edits_as_themselves(client):
    http, service = client
    response = http.put("/jd-profile-templates/FRESHER", json=_BODY, headers=_as("HR_ADMIN"))
    assert response.status_code == 200 and response.json()["data"]["changed"] is True
    assert service.update.call_args.kwargs["actor_id"] == "hr_admin-1"


def test_only_the_three_templates_exist(client):
    http, service = client
    assert http.put("/jd-profile-templates/SENIOR", json=_BODY, headers=_as("HR_ADMIN")).status_code == 422
    service.update.assert_not_called()
