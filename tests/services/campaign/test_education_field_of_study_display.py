from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.candidate_scoring_service import describe_education_gap

"""
Field-of-study wording in the rejection reason / education gap and in the
/deterministic tab's education_validation block. Breakdowns stored before
field matching (no level_passed/field_passed/field_match keys) must read
exactly as before.
"""


def _failed(**overrides):
    result = {
        "passed": False, "required_level": "BACHELOR", "candidate_level": "BACHELOR",
        "level_passed": True, "field_passed": False, "field_match": "MISMATCH",
        "required_field": "COMPUTER_SCIENCE", "required_field_text": "Computer Science",
        "candidate_field": "MECHANICAL_ENGINEERING", "candidate_field_text": None,
        "related_field_allowed": False,
    }
    result.update(overrides)
    return result


def test_field_only_failure_describes_field_not_degree():
    level_gap, field_gap = describe_education_gap(_failed())

    assert level_gap is None
    assert field_gap == "Computer Science required, Mechanical Engineering found"


def test_both_halves_failed():
    level_gap, field_gap = describe_education_gap(_failed(required_level="MASTER", level_passed=False))

    assert level_gap == "Master's required, Bachelor's found"
    assert field_gap is not None


def test_legacy_breakdown_reads_as_degree_level_failure():
    legacy = {"passed": False, "required_level": "MASTER", "candidate_level": "BACHELOR"}

    assert describe_education_gap(legacy) == ("Master's required, Bachelor's found", None)
    assert CampaignCandidateService._education_gap_display({"education_validation": legacy}) == (
        "Master's required, Bachelor's found"
    )


def test_passed_result_has_no_gap():
    assert describe_education_gap({"passed": True}) == (None, None)
    assert CampaignCandidateService._education_gap_display({"education_validation": {"passed": True}}) == ""


def test_gap_display_includes_field_of_study():
    display = CampaignCandidateService._education_gap_display({"education_validation": _failed()})

    assert display == "field of study: Computer Science required, Mechanical Engineering found"


def test_endpoint_field_detail():
    detail = CampaignCandidateService._education_field_detail(_failed())

    assert detail == {
        "required_field": "Computer Science",
        "candidate_field": "Mechanical Engineering",
        "field_match": "MISMATCH",
        "related_field_allowed": False,
        "degree_passed": True,
        "field_passed": False,
    }
    assert CampaignCandidateService._education_field_detail({"passed": True}) == {}   # legacy: fields stay None
