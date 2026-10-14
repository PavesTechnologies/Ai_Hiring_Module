"""Adding / removing JD skills in calibration is HR_ADMIN only (over HTTP)."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.jd_unknown_skills import get_jd_calibration_skill_service
from app.enums.constants import API_PREFIX
from app.schemas.jd.calibration import JDCalibrationSkillChangeResponse, JDCalibrationView

_payload = {}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    response = JDCalibrationSkillChangeResponse(
        action="ADDED", jd_skill_id=uuid4(), canonical_skill_id=uuid4(), skill="Incident Management",
        created_new_skill=False, calibration_version=2, candidates_marked_out_of_date=0,
        calibration=JDCalibrationView.model_construct())
    service.add.return_value = response
    service.remove.return_value = response
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_jd_calibration_skill_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _as(role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return {"Authorization": "Bearer t"}


def _add(http, role):
    return http.post(f"/job-descriptions/{uuid4()}/calibration/skills",
                     json={"canonical_skill_id": str(uuid4()), "tier": "CORE"}, headers=_as(role))


def _remove(http, role):
    return http.delete(f"/job-descriptions/{uuid4()}/calibration/skills/{uuid4()}", headers=_as(role))


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_only_hr_admin_can_add_or_remove(client, role):
    http, service = client
    assert _add(http, role).status_code == 403 and _remove(http, role).status_code == 403
    service.add.assert_not_called()
    service.remove.assert_not_called()


def test_hr_admin_adds_and_removes(client):
    http, service = client
    assert _add(http, "HR_ADMIN").status_code == 200
    assert _remove(http, "HR_ADMIN").status_code == 200
    assert service.add.call_args.kwargs["actor_id"] == "hr_admin-1"


def test_a_request_naming_no_skill_is_rejected(client):
    http, service = client
    response = http.post(f"/job-descriptions/{uuid4()}/calibration/skills", json={"tier": "CORE"},
                         headers=_as("HR_ADMIN"))
    assert response.status_code == 422
    service.add.assert_not_called()
