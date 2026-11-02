"""The recruiter-overrides report against real Postgres (rolled back): totals, patterns and the override rate."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import select

from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.pipeline import PipelineStage
from app.models.scoring_v2 import CandidateScoringResult
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.outcome_override_report import campaign_override_rate, override_report
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def test_the_report_counts_kinds_patterns_skills_and_the_rate(db, scored):  # noqa: F811
    cc, campaign = scored["cc"], scored["campaign"]
    result = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                            CandidateScoringResult.acted_on.is_(True))).scalar_one()
    common = dict(campaign_candidate_id=cc.id, campaign_id=campaign.id, scoring_result_id=result.id)
    db.add_all([
        CandidateOutcomeOverride(**common, system_outcome="PASS", kind="OVERRIDE", direction="DOWN",
                                 from_stage=PipelineStage.SHORTLISTED, to_stage=PipelineStage.REJECTED,
                                 reason_code="ROLE_FIT", missing_core_skills=["MLOps"], profile_template="EXPERIENCED"),
        CandidateOutcomeOverride(**common, system_outcome="HOLD", kind="REVIEW_DECISION", direction="UP",
                                 from_stage=PipelineStage.HOLD, to_stage=PipelineStage.HM_REVIEW,
                                 reason_code="ROLE_FIT", failed_checks=["Experience"]),
    ])
    db.flush()

    report = override_report(db, campaign_id=campaign.id)

    # The fixture already holds one REJECT -> SCREENING override (SKILL_EVIDENCE_MISSED).
    assert (report["total"], report["overrides"], report["review_decisions"]) == (3, 2, 1)
    assert report["by_reason"][0] == {"reason_code": "ROLE_FIT", "reason": "Role fit", "count": 2}
    assert {"skill": "MLOps", "count": 1} in report["by_missing_core_skill"]
    assert {"check": "Experience", "count": 1} in report["by_failed_check"]
    assert any(p["kind"] == "REVIEW_DECISION" and p["system_outcome"] == "HOLD" for p in report["patterns"])
    [row] = report["by_campaign"]
    assert row["campaign"] == campaign.name and row["decided_candidates"] == 1
    assert row["overridden_candidates"] == 1 and row["override_rate"] == 1.0   # one candidate, twice
    assert override_report(db, campaign_id=campaign.id, since=result.created_at.replace(year=2999))["total"] == 0


def _config(threshold):
    config = MagicMock()
    config.get_configs_by_keys.return_value = {"OVERRIDE_RATE_ALERT_THRESHOLD": threshold}
    return config


def test_the_alert_flags_a_campaign_above_the_threshold_and_rule3_agrees(db, scored):  # noqa: F811
    cc, campaign = scored["cc"], scored["campaign"]
    # The fixture's one decided candidate holds one override: 100%.
    assert campaign_override_rate(db, campaign.id) == 100.0

    [flagged] = override_report(db, campaign_id=campaign.id, config_repository=_config("20"))["by_campaign"]
    assert flagged["override_alert"] is True and flagged["recommendation"]
    report = override_report(db, campaign_id=campaign.id, config_repository=_config("100"))
    assert report["override_rate_alert_threshold"] == 100.0
    assert report["by_campaign"][0]["override_alert"] is False   # not above 100%
    assert report["by_campaign"][0]["recommendation"] is None

    # The rejection analytics' Rule 3 reads the same rate.
    service = CampaignCandidateService(campaign_repo=MagicMock(), campaign_candidate_repo=SimpleNamespace(db=db),
                                       audit_service=MagicMock(), config_repo=_config("20"))
    assert service._compute_rule3_override_rate(campaign.id) == (100.0, 20.0)


def test_the_rate_is_zero_without_decided_candidates(db, scored):  # noqa: F811
    db.execute(CandidateScoringResult.__table__.update()
               .where(CandidateScoringResult.campaign_candidate_id == scored["cc"].id).values(acted_on=False))
    assert campaign_override_rate(db, scored["campaign"].id) == 0.0
