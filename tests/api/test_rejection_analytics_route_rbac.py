"""
GET /campaign-candidates/campaign/{id}/rejection-analytics had no role check at
all (any authenticated user). It is HR_ADMIN only now, like the other
rejection-analytics endpoints. Checked over HTTP, not just the route metadata.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.enums.constants import API_PREFIX

_payload = {}


def _decode(token):
    return _payload


@pytest.fixture
def client():
    from app.main import app
    service = MagicMock()
    service.get_campaign_rejection_analytics.return_value = SimpleNamespace(
        campaign_id=uuid4(), total_candidates=0, total_deterministic_rejections=0,
        min_candidates_for_analytics=20, breakdown=[], top_missing_skills=[], recommendations=[])
    with patch("app.middleware.jwt_middleware.decode_token", _decode), \
            patch("app.middleware.rbac._ensure_local_user"):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_campaign_candidate_service] = lambda: service
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=False), service
    app.dependency_overrides.clear()


def _get(http, role):
    _payload.clear()
    _payload.update({"user_id": f"{role.lower()}-1", "email": "u@example.invalid", "roles": [role]})
    return http.get(f"/campaign-candidates/campaign/{uuid4()}/rejection-analytics",
                    headers={"Authorization": "Bearer t"})


@pytest.mark.parametrize("role", ["RECRUITER", "HIRING_MANAGER"])
def test_non_hr_roles_are_refused(client, role):
    http, service = client
    assert _get(http, role).status_code == 403
    service.get_campaign_rejection_analytics.assert_not_called()


def test_hr_admin_gets_the_analytics(client):
    http, service = client
    response = _get(http, "HR_ADMIN")
    assert response.status_code == 200, response.text
    service.get_campaign_rejection_analytics.assert_called_once()
