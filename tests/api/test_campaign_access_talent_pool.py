"""
Batch 3 follow-up: the talent pool search stays open to the whole pool, but stages, scores and
pipeline history come only from campaigns the caller may see; the dashboard's candidate-revealing
panels (top candidates, the activity feed) use the same rule. HR_ADMIN sees every campaign.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from app.db.session import get_db
from app.enums.constants import API_PREFIX
from tests.support.campaign_access_world import access_world


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


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


ROLES = [("rec-1", "RECRUITER", "a"), ("hm-1", "HIRING_MANAGER", "a"), ("rec-2", "RECRUITER", ""),
         ("hr-1", "HR_ADMIN", None)]


# -- talent pool routes ---------------------------------------------------------------------------

@pytest.fixture
def talent_pool(http):
    from app.dependencies.talent_pool import get_talent_pool_service
    from app.schemas.talent_pool.talent_pool_schema import (
        EducationFilterOptions, TalentPoolFiltersResponse, TalentPoolSearchResponse,
        TalentPoolSemanticSearchResponse)
    app, client, world, as_user = http
    service = MagicMock()
    service.search_candidates.return_value = TalentPoolSearchResponse(items=[], total=0, page=1, size=6)
    service.semantic_search_candidates.return_value = TalentPoolSemanticSearchResponse(
        items=[], total=0, page=1, size=6)
    service.get_search_filters.return_value = TalentPoolFiltersResponse(
        education=EducationFilterOptions())
    app.dependency_overrides[get_talent_pool_service] = lambda: service
    return client, world, as_user, service


@pytest.mark.parametrize("user_id, role, sees", ROLES)
def test_the_search_passes_only_the_callers_campaigns(talent_pool, user_id, role, sees):
    client, world, as_user, service = talent_pool
    response = client.get("/talent-pool/candidates", headers=as_user(user_id, role))
    assert response.status_code == 200, response.text
    expected = None if sees is None else ([world.campaign_a.id] if sees else [])
    assert service.search_candidates.call_args.kwargs["visible_campaign_ids"] == expected


@pytest.mark.parametrize("query", ["campaign_ids", "campaign_id"])
def test_the_search_refuses_a_campaign_the_caller_cannot_see(talent_pool, query):
    client, world, as_user, service = talent_pool
    theirs = client.get("/talent-pool/candidates", params={query: str(world.campaign_b.id)},
                        headers=as_user("rec-1", "RECRUITER"))
    assert theirs.status_code == 403
    service.search_candidates.assert_not_called()
    mine = client.get("/talent-pool/candidates", params={query: str(world.campaign_a.id)},
                      headers=as_user("rec-1", "RECRUITER"))
    assert mine.status_code == 200, mine.text
    every = client.get("/talent-pool/candidates", params={query: str(world.campaign_b.id)},
                       headers=as_user("hr-1", "HR_ADMIN"))
    assert every.status_code == 200, every.text


def test_the_semantic_search_refuses_a_campaign_filter_the_caller_cannot_see(talent_pool):
    client, world, as_user, service = talent_pool
    body = {"query": "python developer", "filters": {"campaign_ids": [str(world.campaign_b.id)]}}
    assert client.post("/talent-pool/semantic-search", json=body,
                       headers=as_user("hm-1", "HIRING_MANAGER")).status_code == 403
    service.semantic_search_candidates.assert_not_called()
    response = client.post("/talent-pool/semantic-search", json={"query": "python developer"},
                           headers=as_user("hm-1", "HIRING_MANAGER"))
    assert response.status_code == 200, response.text
    assert service.semantic_search_candidates.call_args.kwargs["visible_campaign_ids"] == [world.campaign_a.id]


def test_the_filter_options_pass_the_callers_campaigns(talent_pool):
    client, world, as_user, service = talent_pool
    assert client.get("/talentpoolfilters", headers=as_user("rec-1", "RECRUITER")).status_code == 200
    assert service.get_search_filters.call_args.args == ([world.campaign_a.id],)


def test_the_profile_route_passes_the_callers_campaigns(talent_pool):
    client, world, as_user, service = talent_pool
    service.get_candidate_profile.side_effect = AssertionError("stop here")   # only the call matters
    with pytest.raises(AssertionError):
        client.get(f"/talent-pool/candidates/{uuid4()}", headers=as_user("hr-1", "HR_ADMIN"))
    assert service.get_candidate_profile.call_args.kwargs["visible_campaign_ids"] is None


# -- talent pool service and SQL ------------------------------------------------------------------

def _service():
    from app.services.talent_pool.talent_pool_service import TalentPoolService
    service = TalentPoolService.__new__(TalentPoolService)
    service.campaign_candidate_repo = MagicMock()
    service.resume_repo = MagicMock(get_top_skills_by_candidate=MagicMock(return_value=["SQL"]))
    service.cache_service = None
    return service


def test_the_profile_summary_counts_only_visible_campaigns():
    from app.models.pipeline import PipelineStage
    mine, theirs = uuid4(), uuid4()
    rows = [  # most recent first: their campaign is newer and scored higher
        (SimpleNamespace(campaign_id=theirs, composite_score=95, pipeline_stage=PipelineStage.SELECTED),
         "Theirs", "JD-T"),
        (SimpleNamespace(campaign_id=mine, composite_score=60, pipeline_stage=PipelineStage.SCREENING),
         "Mine", "JD-M"),
    ]
    service = _service()
    service.campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = rows

    campaigns, performance = service._build_campaign_and_performance_summary(uuid4(), [mine])
    assert (campaigns.total_campaigns, campaigns.latest_campaign) == (1, "Mine")
    assert campaigns.latest_pipeline_stage == PipelineStage.SCREENING
    assert (performance.best_composite_score, performance.campaign_name, performance.selected_count) == (60, "Mine", 0)
    assert performance.top_5_skills == ["SQL"]          # profile-level, always shown

    none_visible, _ = service._build_campaign_and_performance_summary(uuid4(), [])
    assert none_visible.total_campaigns == 0 and none_visible.latest_campaign is None

    every, every_perf = service._build_campaign_and_performance_summary(uuid4(), None)
    assert every.total_campaigns == 2 and every_perf.best_composite_score == 95


def test_the_filter_options_list_only_visible_campaigns():
    from app.schemas.talent_pool.talent_pool_schema import (
        CampaignFilterOption, EducationFilterOptions, TalentPoolFiltersResponse)
    mine, theirs = uuid4(), uuid4()
    service = _service()
    service._load_search_filters = lambda: TalentPoolFiltersResponse(
        education=EducationFilterOptions(),
        campaigns=[CampaignFilterOption(id=mine, name="Mine"), CampaignFilterOption(id=theirs, name="Theirs")])
    assert [c.name for c in service.get_search_filters([mine]).campaigns] == ["Mine"]
    assert [c.name for c in service.get_search_filters(None).campaigns] == ["Mine", "Theirs"]


def test_the_campaign_stage_and_score_filters_look_only_at_visible_campaigns():
    from app.models.pipeline import PipelineStage
    from app.repositories.resume_repository import ResumeRepository
    visible = uuid4()
    repo = ResumeRepository(MagicMock())
    common = dict(search=None, or_skill_terms=None, designation_terms=None, location_terms=None,
                  degree_levels=None, education_fields=None, exclude_campaign_id=None,
                  experience_min=None, experience_max=None, score_max=None, resolved_skill_ids_by_term={},
                  campaign_ids=[uuid4()], pipeline_stages=[PipelineStage.SELECTED], score_min=50)
    scoped = repo._talent_pool_filter_conditions(**common, visible_campaign_ids=[visible])
    campaign_sql, stage_sql, score_sql = (_sql(c) for c in scoped)
    assert campaign_sql.count("campaign_candidates.campaign_id in") == 2       # the named campaigns + visible
    assert "pipeline_stage in" in stage_sql and "campaign_candidates.campaign_id in" in stage_sql
    assert "max(campaign_candidates.composite_score)" in score_sql
    assert "campaign_candidates.campaign_id in" in score_sql
    every = repo._talent_pool_filter_conditions(**common)
    stage_sql = _sql(every[1])
    assert "pipeline_stage in" in stage_sql and "campaign_candidates.campaign_id in" not in stage_sql


def test_the_card_best_score_looks_only_at_visible_campaigns():
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    db = MagicMock()
    repo = CampaignCandidateRepository(db)
    repo.get_best_composite_scores_by_candidate_ids([uuid4()], visible_campaign_ids=[uuid4()])
    assert "campaign_candidates.campaign_id in" in _sql(db.execute.call_args.args[0])
    repo.get_best_composite_scores_by_candidate_ids([uuid4()])
    assert "campaign_candidates.campaign_id in" not in _sql(db.execute.call_args.args[0])


# -- dashboard panels -----------------------------------------------------------------------------

@pytest.mark.parametrize("path, method", [("/dashboard/top-candidates", "get_top_candidates"),
                                          ("/dashboard/notifications", "get_notifications")])
@pytest.mark.parametrize("user_id, role, sees", ROLES)
def test_the_candidate_revealing_dashboard_panels_pass_the_callers_campaigns(http, path, method,
                                                                             user_id, role, sees):
    from app.dependencies.dashboard import get_dashboard_service
    from app.schemas.dashboard.dashboard_response import NotificationsFeedResponse, TopCandidatesResponse
    app, client, world, as_user = http
    service = MagicMock()
    service.get_top_candidates.return_value = TopCandidatesResponse(candidates=[])
    service.get_notifications.return_value = NotificationsFeedResponse(items=[])
    app.dependency_overrides[get_dashboard_service] = lambda: service
    response = client.get(path, headers=as_user(user_id, role))
    assert response.status_code == 200, response.text
    expected = None if sees is None else ([world.campaign_a.id] if sees else [])
    assert getattr(service, method).call_args.kwargs["visible_campaign_ids"] == expected


@pytest.fixture
def statements():
    """DashboardRepository queries compiled instead of run."""
    with patch.object(Query, "all", lambda self: [_sql(self.statement)]):
        yield


def test_top_candidates_and_the_feed_filter_on_visible_campaigns(statements):
    from app.repositories.dashboard_repository import DashboardRepository
    repo = DashboardRepository(Session())
    [scoped] = repo.get_top_candidates(5, visible_campaign_ids=[uuid4()])
    [every] = repo.get_top_candidates(5)
    assert "campaign_candidates.campaign_id in" in scoped and "campaign_candidates.campaign_id in" not in every

    [feed] = repo.get_recent_audit_entries(10, action_types=["CAMPAIGN_CREATED"], visible_campaign_ids=[uuid4()])
    assert "audit_log.campaign_id is null" in feed.replace("audit_logs", "audit_log")
    assert "campaign_id in" in feed
    [every_feed] = repo.get_recent_audit_entries(10, action_types=["CAMPAIGN_CREATED"])
    assert "campaign_id is null" not in every_feed
