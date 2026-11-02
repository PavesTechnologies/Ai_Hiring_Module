"""
M07-E03 S05 - integration + regression coverage.

Integration: cross-feature consistency guards that unit tests mocking each
method in isolation can't catch - the campaign-level breakdown (T01) must
classify rejections the same way the platform-wide export's "top rejection
reason" column does (T03), since both call the same _classify_rejection
method. (Rule 3's agreement with the override-rate alert is checked against
real Postgres in tests/db/test_outcome_override_report.py.)

Regression: the constructor extension (new optional skill_repo param)
does not break the one pre-existing call site that constructs this
service with only 3 positional args (app/tasks/bulk_upload_tasks.py).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.campaign.campaign_candidate_service import CampaignCandidateService


def _make_rejection(mandatory_skills=None, experience_validation=None, education_validation=None):
    # decision_details snapshot, as campaign_repo.get_deterministic_rejection_details returns it.
    return {
        "mandatory_skills": mandatory_skills or [],
        "experience_validation": experience_validation,
        "education_validation": education_validation,
    }


def test_breakdown_classification_matches_export_top_rejection_reason_for_the_same_campaign():
    campaign = SimpleNamespace(id=uuid4(), name="Same Logic Campaign")
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_all_campaigns.return_value = [campaign]

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_count.return_value = 20
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []

    # 3 SKILLS_ONLY, 1 EXPERIENCE_ONLY - SKILLS_ONLY should dominate both
    # the campaign breakdown AND the export's "top rejection reason".
    rejections = [
        _make_rejection(mandatory_skills=[{
            "canonical_skill_id": uuid4(), "canonical_name": "Python", "match_type": "MISSING",
        }])
        for _ in range(3)
    ] + [
        _make_rejection(experience_validation={"passed": False, "candidate_years": 2.0, "min_years": 4.0}),
    ]
    campaign_repo.get_deterministic_rejection_details.return_value = [(r, campaign.id) for r in rejections]

    service = CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
    )

    analytics = service.get_campaign_rejection_analytics(campaign.id)
    dominant_from_t01 = max(analytics.breakdown, key=lambda entry: entry.count)

    summary_row = service._to_campaign_summary_row(campaign, rejections, date_from=None, date_to=None)

    assert dominant_from_t01.category == "SKILLS_ONLY"
    assert summary_row["top_rejection_reason"] == "Missing Skills Only"


def test_service_still_constructible_with_only_the_original_three_positional_args():
    """
    Matches app/tasks/bulk_upload_tasks.py's exact call:
    CampaignCandidateService(campaign_repo, campaign_candidate_repo, audit_service)
    """
    service = CampaignCandidateService(MagicMock(), MagicMock(), MagicMock())

    assert service.skill_repo is None
    assert service.config_repo is None
