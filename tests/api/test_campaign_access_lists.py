"""
A-H5: lists cover only the campaigns the caller may see - the campaign pickers, and the resume
lists with "All Campaigns" (no campaign_id). The resume processing socket checks the task's campaign.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.db.session import get_db
from app.enums.constants import API_PREFIX
from tests.support.campaign_access_world import access_world


@pytest.fixture
def http():
    from app.main import app
    payload = {}
    with access_world() as world, \
            patch("app.middleware.jwt_middleware.decode_token", lambda token: payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        client = TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000))

        def as_user(user_id, role):
            payload.clear()
            payload.update({"user_id": user_id, "email": f"{user_id}@example.invalid", "roles": [role]})
            return {"Authorization": "Bearer t"}

        yield app, client, world, as_user
        app.dependency_overrides.clear()


@pytest.mark.parametrize("user_id, role, sees", [
    ("rec-1", "RECRUITER", "a"), ("rec-c", "RECRUITER", "a"), ("hm-1", "HIRING_MANAGER", "a"),
    ("rec-2", "RECRUITER", ""), ("hr-1", "HR_ADMIN", "ab"),
])
def test_the_active_campaign_picker_lists_only_visible_campaigns(http, user_id, role, sees):
    from app.dependencies.campaign import get_campaign_service
    app, client, world, as_user = http
    service = MagicMock()
    service.get_active_campaigns_minimal.return_value = [
        SimpleNamespace(id=world.campaign_a.id, name="A"), SimpleNamespace(id=world.campaign_b.id, name="B")]
    app.dependency_overrides[get_campaign_service] = lambda: service
    response = client.get("/campaigns/active", headers=as_user(user_id, role))
    assert response.status_code == 200, response.text
    assert "".join(sorted(c["name"].lower() for c in response.json()["data"])) == sees


@pytest.mark.parametrize("path", ["/resumes", "/resumes/pipeline-status"])
@pytest.mark.parametrize("user_id, role, expected", [("rec-1", "RECRUITER", "a"), ("hr-1", "HR_ADMIN", None)])
def test_the_resume_lists_without_a_campaign_are_scoped_to_visible_campaigns(http, path, user_id, role, expected):
    from app.dependencies.resume import get_resume_monitoring_service
    app, client, world, as_user = http
    service = MagicMock()
    app.dependency_overrides[get_resume_monitoring_service] = lambda: service
    client.get(path, headers=as_user(user_id, role))
    method = service.list_resumes if path == "/resumes" else service.list_resumes_with_pipeline_status
    visible = method.call_args.kwargs["visible_campaign_ids"]
    assert visible == (None if expected is None else [world.campaign_a.id])


def test_the_resume_search_filter_reaches_both_links_to_a_campaign():
    from app.repositories.resume_repository import ResumeRepository
    conditions = ResumeRepository._build_search_conditions(None, None, None, None, None, None,
                                                           visible_campaign_ids=[uuid4()])
    sql = str(conditions[0].compile(dialect=postgresql.dialect())).lower()
    assert "campaign_candidates.campaign_id" in sql and "bulk_upload_jobs.campaign_id" in sql
    assert ResumeRepository._build_search_conditions(None, None, None, None, None, None) == []


def test_a_shared_resume_shows_only_the_callers_campaign_link():
    from app.services.resume.resume_monitoring_service import ResumeMonitoringService
    mine, theirs = uuid4(), uuid4()
    resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4())
    service = ResumeMonitoringService.__new__(ResumeMonitoringService)
    service.resume_repository = MagicMock(search=MagicMock(return_value=[resume]),
                                          count_search=MagicMock(return_value=1))
    service.candidate_repository = MagicMock(get_by_ids=MagicMock(return_value=[]))
    links = [SimpleNamespace(resume_id=resume.id, campaign_id=mine, created_at=1),
             SimpleNamespace(resume_id=resume.id, campaign_id=theirs, created_at=2)]   # theirs is newer
    service.campaign_candidate_repository = MagicMock(get_by_resume_ids=MagicMock(return_value=links))
    *_, by_resume = service._fetch_resume_page(
        campaign_id=None, parse_status=None, source=None, email_hash=None, uploaded_from=None, uploaded_to=None,
        page=1, size=20, sort_by="created_at", sort_dir="desc", visible_campaign_ids=[mine])
    assert by_resume[resume.id].campaign_id == mine
    assert service.resume_repository.search.call_args.kwargs["visible_campaign_ids"] == [mine]


@pytest.mark.parametrize("user_id, role, allowed", [
    ("rec-1", "RECRUITER", True), ("rec-2", "RECRUITER", False), ("hr-1", "HR_ADMIN", True),
])
def test_who_may_watch_a_resume_processing_task(user_id, role, allowed):
    from app.middleware.rbac import TokenUser
    from app.websocket import router as ws_router
    with access_world() as world, patch("app.db.session.SessionLocal", MagicMock()):
        user = TokenUser(user_id=user_id, email=None, roles=[role])
        assert ws_router._may_watch_task(user, world.task_a) is allowed
