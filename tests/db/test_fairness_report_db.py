"""The fairness report against real Postgres (rolled back): it reads the current decision and the resume's parse."""
from unittest.mock import MagicMock

from sqlalchemy import select

from app.models.scoring_v2 import CandidateScoringResult
from app.services.monitoring.fairness_report import fairness_report
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def test_one_decided_candidate_is_counted_and_hidden_below_the_group_size(db, scored):  # noqa: F811
    cc, campaign, resume = scored["cc"], scored["campaign"], scored["resume"]
    result = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                            CandidateScoringResult.acted_on.is_(True))).scalar_one()
    result.outcome = "PASS"
    result.breakdown = {"experience_validation": {"candidate_years": 6.0, "passed": True}}
    resume.parsed_json = {"location": "Hyderabad", "education": [{"degree_level": "BACHELOR"}],
                          "work_experience": [{"start_date": "Jan 2019", "end_date": "Dec 2024"}]}
    db.flush()
    config = MagicMock()
    config.get_configs_by_keys.return_value = {"FAIRNESS_MIN_GROUP_SIZE": "2"}

    report = fairness_report(db, config, campaign_id=campaign.id)

    assert (report["candidates"], report["meeting_min_experience"], report["min_group_size"]) == (1, 1, 2)
    region = next(d for d in report["dimensions"] if d["key"] == "region")
    assert region["all"] == [{"group": "Telangana, India", "candidates": None, "suppressed": True, "pass_rate": None,
                              "pass_or_hold_rate": None, "ratio_to_best": None, "below_four_fifths": False}]
