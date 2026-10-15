"""Certification -> skill mappings are HR_ADMIN only (over HTTP)."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.routes.certification_routes import get_certification_mapping_service
from app.db.session import get_db
from app.enums.constants import API_PREFIX

_payload = {}


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.list_all.return_value = []
    with patch("app.middleware.jwt_middleware.decode_token", lambda token: _payload), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_certification_mapping_service] = lambda: service
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
    cert, skill = uuid4(), uuid4()
    assert http.get("/certifications", headers=_as(role)).status_code == 403
    assert http.get("/certifications/meanings", headers=_as(role)).status_code == 403
    assert http.post("/certifications", json={"name": "PMP"}, headers=_as(role)).status_code == 403
    assert http.delete(f"/certifications/{cert}", headers=_as(role)).status_code == 403
    assert http.post(f"/certifications/{cert}/skills", json={"canonical_skill_id": str(skill)},
                     headers=_as(role)).status_code == 403
    assert http.delete(f"/certifications/{cert}/skills/{skill}", headers=_as(role)).status_code == 403
    for method in ("add_certification", "remove_certification", "add_skill", "remove_skill"):
        getattr(service, method).assert_not_called()


def test_hr_admin_maps_a_skill(client):
    http, service = client
    cert, skill = uuid4(), uuid4()
    response = http.post(f"/certifications/{cert}/skills", json={"canonical_skill_id": str(skill), "sense_key": "ITSM"},
                         headers=_as("HR_ADMIN"))
    assert response.status_code == 200
    assert service.add_skill.call_args.args[0] == cert
    assert service.add_skill.call_args.kwargs["canonical_skill_id"] == skill
    assert service.add_skill.call_args.kwargs["sense_key"] == "ITSM"


def test_hr_admin_gets_the_meanings_of_ambiguous_skills_only(client):
    from types import SimpleNamespace
    from app.services.skills.skill_sense_resolver import SenseDef
    http, service = client
    cm, py = uuid4(), uuid4()
    service.senses_loader.return_value = [SenseDef(cm, "ITSM", "IT service change", "", ()),
                                          SenseDef(cm, "ORGANISATIONAL", "People side", "", ()),
                                          SenseDef(py, "ONLY", "Single", "", ())]
    service.skill_repository.get_skill_by_id.return_value = SimpleNamespace(canonical_name="Change Management")
    data = http.get("/certifications/meanings", headers=_as("HR_ADMIN")).json()["data"]
    assert [(d["skill"], [s["sense_key"] for s in d["senses"]]) for d in data] == [
        ("Change Management", ["ITSM", "ORGANISATIONAL"])]
