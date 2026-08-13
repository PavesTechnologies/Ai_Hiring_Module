from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import PipelineStage
from app.services.talent_pool.talent_pool_service import TalentPoolService

"""GET /talentpoolfilters - TalentPoolService.get_search_filters assembles
filter option metadata for the Talent Pool Normal Search UI. Never performs
a candidate search; only aggregates already-persisted distinct values."""


def _default_list(mock_method):
    """Only sets a default when the caller hasn't already configured this mock's return_value."""
    if not isinstance(mock_method.return_value, list):
        mock_method.return_value = []


def _make_service(resume_repo=None, campaign_repo=None):
    resume_repo = resume_repo or MagicMock()
    _default_list(resume_repo.get_distinct_locations)
    _default_list(resume_repo.get_distinct_designations)
    _default_list(resume_repo.get_distinct_education_degree_levels)
    _default_list(resume_repo.get_distinct_education_fields)

    campaign_repo = campaign_repo or MagicMock()
    _default_list(campaign_repo.get_active_campaigns_minimal)

    return TalentPoolService(
        candidate_repo=MagicMock(),
        resume_repo=resume_repo,
        campaign_repo=campaign_repo,
        campaign_candidate_repo=MagicMock(),
        consent_repo=MagicMock(),
        encryption_service=MagicMock(),
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=MagicMock(),
        skill_repo=MagicMock(),
    )


def test_returns_empty_lists_when_no_data_exists():
    service = _make_service()

    result = service.get_search_filters()

    assert result.locations == []
    assert result.designations == []
    assert result.education.degree_levels == []
    assert result.education.fields == []
    assert result.campaigns == []


def test_pipeline_stages_come_from_the_existing_enum_in_declaration_order():
    service = _make_service()

    result = service.get_search_filters()

    assert result.pipeline_stages == [stage.value for stage in PipelineStage]


def test_locations_are_deduped_case_insensitively_keeping_most_frequent_casing():
    resume_repo = MagicMock()
    resume_repo.get_distinct_locations.return_value = [
        ("hyderabad", 1),
        ("HYDERABAD", 2),
        ("Hyderabad", 5),
        ("Chennai", 1),
    ]
    resume_repo.get_distinct_designations.return_value = []
    resume_repo.get_distinct_education_degree_levels.return_value = []
    resume_repo.get_distinct_education_fields.return_value = []
    service = _make_service(resume_repo=resume_repo)

    result = service.get_search_filters()

    assert result.locations == ["Chennai", "Hyderabad"]


def test_designations_are_deduped_case_insensitively():
    resume_repo = MagicMock()
    resume_repo.get_distinct_locations.return_value = []
    resume_repo.get_distinct_designations.return_value = [
        ("python developer", 1),
        ("Python Developer", 4),
    ]
    resume_repo.get_distinct_education_degree_levels.return_value = []
    resume_repo.get_distinct_education_fields.return_value = []
    service = _make_service(resume_repo=resume_repo)

    result = service.get_search_filters()

    assert result.designations == ["Python Developer"]


def test_education_unknown_sentinel_is_excluded():
    resume_repo = MagicMock()
    resume_repo.get_distinct_locations.return_value = []
    resume_repo.get_distinct_designations.return_value = []
    resume_repo.get_distinct_education_degree_levels.return_value = ["BACHELOR", "UNKNOWN", "MASTER"]
    resume_repo.get_distinct_education_fields.return_value = ["COMPUTER_SCIENCE", "UNKNOWN"]
    service = _make_service(resume_repo=resume_repo)

    result = service.get_search_filters()

    assert result.education.degree_levels == ["BACHELOR", "MASTER"]
    assert result.education.fields == ["COMPUTER_SCIENCE"]


def test_campaigns_return_id_and_name_from_existing_repository_method():
    campaign_id = uuid4()
    campaign_repo = MagicMock()
    campaign_repo.get_active_campaigns_minimal.return_value = [(campaign_id, "Python Developer Hiring")]
    service = _make_service(campaign_repo=campaign_repo)

    result = service.get_search_filters()

    assert len(result.campaigns) == 1
    assert result.campaigns[0].id == campaign_id
    assert result.campaigns[0].name == "Python Developer Hiring"
