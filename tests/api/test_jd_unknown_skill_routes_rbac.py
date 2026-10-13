"""
JD unknown-skill routes (Phase 2 Step 2), over HTTP: resolving is HR_ADMIN
only; the top-3 suggestions are readable by every role that works with JDs.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.jd_unknown_skills import get_jd_unknown_skill_service
from app.enums.constants import API_PREFIX
from app.schemas.jd.calibration import JDCalibrationView, JDUnknownSkillResolveResponse

_payload = {}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.suggestions.return_value = []
    service.resolve.return_value = JDUnknownSkillResolveResponse(
        action="DROP", unknown_skill_id=uuid4(), canonical_skill_id=None, affected_jd_ids=[],
        candidates_marked_out_of_date=0, calibration=JDCalibrationView.model_construct())
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_jd_unknown_skill_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


def _resolve(http, role):
    return http.post(f"/job-descriptions/{uuid4()}/unknown-skills/{uuid4()}/resolve",
                     json={"action": "DROP"}, headers=_as(role))


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_only_hr_admin_can_resolve(client, role):
    http, service = client
    assert _resolve(http, role).status_code == 403
    service.resolve.assert_not_called()


def test_hr_admin_resolves(client):
    http, service = client
    response = _resolve(http, "HR_ADMIN")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Dropped from this JD."
    assert service.resolve.call_args.kwargs["actor_id"] == "hr_admin-1"


@pytest.mark.parametrize("role", ["HR_ADMIN", "RECRUITER", "HIRING_MANAGER"])
def test_suggestions_are_readable_by_jd_roles(client, role):
    http, service = client
    response = http.get(f"/job-descriptions/{uuid4()}/unknown-skills/suggestions", headers=_as(role))
    assert response.status_code == 200, response.text


def test_an_unknown_action_is_rejected(client):
    http, service = client
    response = http.post(f"/job-descriptions/{uuid4()}/unknown-skills/{uuid4()}/resolve",
                         json={"action": "PROMOTE"}, headers=_as("HR_ADMIN"))
    assert response.status_code == 422
    service.resolve.assert_not_called()
