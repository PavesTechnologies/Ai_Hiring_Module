from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.schemas.campaign.campaign_candidate_schema import (
    CampaignRejectionAnalyticsResponse,
    OverrideReportResponse,
    OverrideReportRow,
)
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
M07-E03 S05 - Report Deterministic Rejection Analytics.

T01: get_campaign_rejection_analytics - breakdown + top missing skills
T02: JD calibration recommendations (embedded in the same response)
T03: export_deterministic_rejection_summary - platform-wide 3-sheet XLSX
"""


def _make_rejection(mandatory_skills=None, experience_validation=None, education_validation=None):
    detail = {
        "mandatory_skills": mandatory_skills or [],
        "experience_validation": experience_validation,
        "education_validation": education_validation,
    }
    return SimpleNamespace(rejection_detail=detail)


def _missing_skill(canonical_name, canonical_skill_id=None):
    # Deterministic per-name id (uuid5, not uuid4) so repeated calls for the
    # SAME skill name aggregate together - mirrors production reality
    # (a canonical_skill_id is stable for a given skill), unlike a fresh
    # random uuid4() per call which would make every occurrence look like a
    # distinct skill.
    import uuid
    return {
        "canonical_skill_id": canonical_skill_id or uuid.uuid5(uuid.NAMESPACE_OID, canonical_name),
        "canonical_name": canonical_name,
        "match_type": "MISSING",
    }


def _matched_skill(canonical_name):
    return {"canonical_skill_id": uuid4(), "canonical_name": canonical_name, "match_type": "EXACT"}


def _failed_experience(candidate_years=2.0, min_years=4.0):
    return {"passed": False, "candidate_years": candidate_years, "min_years": min_years}


def _passed_experience():
    return {"passed": True, "candidate_years": 5.0, "min_years": 4.0}


def _failed_education():
    return {"passed": False, "required_level": "BACHELOR", "candidate_level": "HIGH_SCHOOL"}


def make_service(
    campaign_repo=None,
    campaign_candidate_repo=None,
    candidate_rejection_repo=None,
    config_repo=None,
    skill_repo=None,
    audit_service=None,
):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
        skill_repo=skill_repo,
    )


# ----------------------------------------------------------------------
# T01 - Campaign Rejection Analytics
# ----------------------------------------------------------------------

def test_analytics_not_found_when_campaign_missing():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = None
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_campaign_rejection_analytics(uuid4())

    assert exc_info.value.status_code == 404


def test_analytics_empty_campaign_returns_zeroed_breakdown():
    campaign = SimpleNamespace(id=uuid4(), name="Backend Engineer", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 0
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = []

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert isinstance(analytics, CampaignRejectionAnalyticsResponse)
    assert analytics.total_deterministic_rejections == 0
    assert len(analytics.breakdown) == 7
    assert all(entry.count == 0 and entry.percentage == 0.0 for entry in analytics.breakdown)
    assert analytics.top_missing_skills == []
    assert analytics.recommendations == []


def test_analytics_classifies_each_of_the_seven_categories():
    rejections = [
        _make_rejection(mandatory_skills=[_missing_skill("Python")]),  # SKILLS_ONLY
        _make_rejection(experience_validation=_failed_experience()),  # EXPERIENCE_ONLY
        _make_rejection(education_validation=_failed_education()),  # EDUCATION_ONLY
        _make_rejection(mandatory_skills=[_missing_skill("AWS")], experience_validation=_failed_experience()),  # SKILLS_EXPERIENCE
        _make_rejection(mandatory_skills=[_missing_skill("Docker")], education_validation=_failed_education()),  # SKILLS_EDUCATION
        _make_rejection(experience_validation=_failed_experience(), education_validation=_failed_education()),  # EXPERIENCE_EDUCATION
        _make_rejection(
            mandatory_skills=[_missing_skill("SQL")],
            experience_validation=_failed_experience(),
            education_validation=_failed_education(),
        ),  # SKILLS_EXPERIENCE_EDUCATION
    ]
    campaign = SimpleNamespace(id=uuid4(), name="Data Engineer", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 0
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert analytics.total_deterministic_rejections == 7
    by_category = {entry.category: entry.count for entry in analytics.breakdown}
    assert by_category == {
        "SKILLS_ONLY": 1,
        "EXPERIENCE_ONLY": 1,
        "EDUCATION_ONLY": 1,
        "SKILLS_EXPERIENCE": 1,
        "SKILLS_EDUCATION": 1,
        "EXPERIENCE_EDUCATION": 1,
        "SKILLS_EXPERIENCE_EDUCATION": 1,
    }
    for entry in analytics.breakdown:
        assert entry.percentage == pytest.approx(100 / 7, rel=1e-2)


def test_analytics_edge_case_rejection_with_no_individual_failure_counts_toward_total_only():
    """
    A rejection where none of skills/experience/education individually
    failed (e.g. combined weighted score alone fell below threshold) must
    still count toward total_deterministic_rejections but must not be
    force-fit into any of the 7 named buckets.
    """
    rejections = [
        _make_rejection(mandatory_skills=[_matched_skill("Python")], experience_validation=_passed_experience()),
    ]
    campaign = SimpleNamespace(id=uuid4(), name="Edge Case Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 0
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert analytics.total_deterministic_rejections == 1
    assert sum(entry.count for entry in analytics.breakdown) == 0


def test_analytics_top_missing_skills_sorted_and_percentaged():
    rejections = [
        _make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(3)
    ] + [
        _make_rejection(mandatory_skills=[_missing_skill("AWS")]) for _ in range(1)
    ]
    campaign = SimpleNamespace(id=uuid4(), name="Campaign X", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 0
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert len(analytics.top_missing_skills) == 2
    assert analytics.top_missing_skills[0].canonical_name == "Python"
    assert analytics.top_missing_skills[0].occurrence_count == 3
    assert analytics.top_missing_skills[0].percentage_of_rejections == 75.0
    assert analytics.top_missing_skills[1].canonical_name == "AWS"
    assert analytics.top_missing_skills[1].occurrence_count == 1


def test_analytics_top_missing_skills_capped_at_five():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill(f"Skill{i}")]) for i in range(8)]
    campaign = SimpleNamespace(id=uuid4(), name="Campaign Y", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 0
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert len(analytics.top_missing_skills) == 5


# ----------------------------------------------------------------------
# T02 - JD Calibration Recommendations (gating + rules)
# ----------------------------------------------------------------------

def test_recommendations_empty_below_min_candidates_threshold():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(10)]
    campaign = SimpleNamespace(id=uuid4(), name="Small Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 5  # below default MIN_CANDIDATES_FOR_ANALYTICS (20)
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert analytics.min_candidates_for_analytics == 20
    assert analytics.recommendations == []


def test_recommendations_reads_min_candidates_threshold_from_config():
    campaign = SimpleNamespace(id=uuid4(), name="Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 3
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = []
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"MIN_CANDIDATES_FOR_ANALYTICS": "2"}

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert analytics.min_candidates_for_analytics == 2


def _make_gated_harness(rejections, total_candidates=25):
    campaign = SimpleNamespace(
        id=uuid4(), name="Campaign", job_description=SimpleNamespace(min_experience_years=5.0),
    )
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = total_candidates
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {}

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
    )
    return service, campaign, campaign_candidate_repo


def test_rule1_recommends_when_skill_missing_above_threshold():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(7)] + [
        _make_rejection(mandatory_skills=[_matched_skill("Python")]) for _ in range(3)
    ]
    service, campaign, _ = _make_gated_harness(rejections)

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    rule1 = next((r for r in analytics.recommendations if r.rule == "SKILL_MISMATCH"), None)
    assert rule1 is not None
    assert rule1.action == "review_skill_ontology"
    assert "Python" in rule1.message
    assert "preferred rather than mandatory" in rule1.message
    assert rule1.details["skill"] == "Python"


def test_rule1_does_not_recommend_when_below_threshold():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(5)] + [
        _make_rejection(mandatory_skills=[_matched_skill("Python")]) for _ in range(5)
    ]
    service, campaign, _ = _make_gated_harness(rejections)

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert not any(r.rule == "SKILL_MISMATCH" for r in analytics.recommendations)


def test_rule1_threshold_read_from_config_not_hardcoded():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(5)] + [
        _make_rejection(mandatory_skills=[_matched_skill("Python")]) for _ in range(5)
    ]
    campaign = SimpleNamespace(id=uuid4(), name="Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 25
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]
    config_repo = MagicMock()
    # 50% missing-rate wouldn't clear the ticket's default 60% threshold,
    # but WOULD clear a lowered, config-driven threshold of 40%.
    config_repo.get_configs_by_keys.return_value = {"SKILL_MISMATCH_RATE_THRESHOLD": "40"}

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert any(r.rule == "SKILL_MISMATCH" for r in analytics.recommendations)


def test_rule2_recommends_when_experience_only_above_threshold():
    rejections = [_make_rejection(experience_validation=_failed_experience()) for _ in range(5)] + [
        _make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(5)
    ]
    service, campaign, _ = _make_gated_harness(rejections)

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    rule2 = next((r for r in analytics.recommendations if r.rule == "EXPERIENCE_MISMATCH"), None)
    assert rule2 is not None
    assert rule2.action == "review_campaign_configuration"
    assert rule2.details["current_min_experience_years"] == 5.0
    assert rule2.details["recommended_min_experience_years"] == 4.0


def test_rule2_does_not_recommend_when_below_threshold():
    rejections = [_make_rejection(experience_validation=_failed_experience()) for _ in range(3)] + [
        _make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(7)
    ]
    service, campaign, _ = _make_gated_harness(rejections)

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert not any(r.rule == "EXPERIENCE_MISMATCH" for r in analytics.recommendations)


def test_rule3_recommends_when_override_rate_above_threshold():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(10)]
    campaign = SimpleNamespace(id=uuid4(), name="Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 25
    campaign_candidate_repo.get_overridden.return_value = [1, 2, 3]  # 3 overrides
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [1, 2, 3, 4, 5]  # 5 rejected -> 60%
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"OVERRIDE_RATE_ALERT_THRESHOLD": "20"}

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    rule3 = next((r for r in analytics.recommendations if r.rule == "HIGH_OVERRIDE_RATE"), None)
    assert rule3 is not None
    assert rule3.action == "review_campaign_configuration"
    assert rule3.details["override_rate"] == 60.0


def test_rule3_does_not_recommend_when_override_rate_below_threshold():
    rejections = [_make_rejection(mandatory_skills=[_missing_skill("Python")]) for _ in range(10)]
    campaign = SimpleNamespace(id=uuid4(), name="Campaign", job_description=None)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 25
    campaign_candidate_repo.get_overridden.return_value = [1]
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [1, 2, 3, 4, 5]  # 20%, not > 20%
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)

    assert not any(r.rule == "HIGH_OVERRIDE_RATE" for r in analytics.recommendations)


# ----------------------------------------------------------------------
# T03 - Platform-wide Export
# ----------------------------------------------------------------------

def test_export_deterministic_rejection_summary_produces_a_real_three_sheet_workbook(monkeypatch):
    import asyncio
    import io

    import openpyxl

    campaign = SimpleNamespace(id=uuid4(), name="Backend Engineer")
    campaign_repo = MagicMock()
    campaign_repo.get_all_campaigns.return_value = [campaign]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 10
    campaign_candidate_repo.get_overridden.return_value = [1]  # 1 override, date-scoped
    skill_id = uuid4()
    rejections = [
        _make_rejection(mandatory_skills=[_missing_skill("Python", skill_id)]) for _ in range(2)
    ]
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign.return_value = [(r, campaign.id) for r in rejections]
    skill_repo = MagicMock()
    skill_repo.get_campaign_requirement_counts_by_skill.return_value = {skill_id: 4}
    audit_service = MagicMock()

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        skill_repo=skill_repo,
        audit_service=audit_service,
    )

    override_row = OverrideReportRow(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        candidate_uuid=uuid4(),
        original_rejection_reason="Missing required skills: Python.",
        override_reason="HR approved manually",
        hr_full_name="Jordan HR",
        override_timestamp=datetime.now(timezone.utc),
        current_pipeline_stage="SCREENING",
    )
    monkeypatch.setattr(
        service, "get_override_report",
        lambda campaign_id=None, date_from=None, date_to=None: OverrideReportResponse(
            rows=[override_row], total_count=1, weekly_trend=[], campaign_alerts=[],
        ),
    )

    response = service.export_deterministic_rejection_summary(
        date_from=None, date_to=None, actor_id="hr-1", actor_role="HR_ADMIN",
    )

    async def _drain():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(_drain())
    workbook = openpyxl.load_workbook(io.BytesIO(body))

    assert workbook.sheetnames == ["Campaign Summary", "Skill Gap Analysis", "Override Log"]

    summary_sheet = workbook["Campaign Summary"]
    assert [c.value for c in summary_sheet[1]] == [
        "Campaign Name", "Total Candidates", "Deterministic Rejections",
        "Rejection Rate (%)", "Top Rejection Reason", "Override Count", "Override Rate (%)",
    ]
    summary_row = [c.value for c in summary_sheet[2]]
    assert summary_row[0] == "Backend Engineer"
    assert summary_row[1] == 10
    assert summary_row[2] == 2
    assert summary_row[4] == "Missing Skills Only"
    assert summary_row[5] == 1

    skill_sheet = workbook["Skill Gap Analysis"]
    assert [c.value for c in skill_sheet[1]] == [
        "Skill Canonical Name", "Campaigns Requiring Skill", "Missing Count", "Missing Rate (%)",
    ]
    skill_row = [c.value for c in skill_sheet[2]]
    assert skill_row[0] == "Python"
    assert skill_row[1] == 4
    assert skill_row[2] == 2

    override_sheet = workbook["Override Log"]
    assert [c.value for c in override_sheet[1]] == [
        "Campaign Name", "Candidate UUID", "Rejection Reason", "Override Reason",
        "Override By", "Override Timestamp", "Current Pipeline Stage",
    ]
    override_row_values = [c.value for c in override_sheet[2]]
    assert override_row_values[0] == "Backend Engineer"
    assert override_row_values[3] == "HR approved manually"
    assert override_row_values[4] == "Jordan HR"

    # Never exports PII.
    for sheet in workbook.worksheets:
        header = [c.value for c in sheet[1]]
        for forbidden in ("Candidate Name", "Email", "Phone", "Resume"):
            assert forbidden not in header

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "DETERMINISTIC_ANALYTICS_EXPORTED"
    assert kwargs["entity_id"] == CampaignCandidateService.EXPORT_AUDIT_ENTITY_ID
    assert kwargs["campaign_id"] is None
