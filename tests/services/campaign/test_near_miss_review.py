"""
REVIEW outcome: a near-miss candidate (one core skill over the limit, an
uncertain core match that takes them over it, or experience short within
max(1 year, 10% of the minimum)) is let through to semantic and AI and capped
at HOLD with "Needs recruiter review". Two near-misses reject; zero confirmed
core skills always rejects. Semantic and AI rejections still reject. No email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.pipeline import DecisionSource, DecisionType, PipelineStage
from app.services.campaign.candidate_scoring_service import CandidateScoringService
from app.services.campaign.near_miss import (
    OUTCOME_HOLD, OUTCOME_PASS, OUTCOME_REJECT, evaluate_near_miss, experience_near_miss_tolerance,
    needs_recruiter_review,
)


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")) as send:
        yield send


def _rule(core=4, confirmed=None, allowed=1, missing=0, uncertain=0, experience=None, other=None):
    confirmed = core - missing - uncertain if confirmed is None else confirmed
    return evaluate_near_miss(core_skill_count=core, confirmed_core_count=confirmed, allowed_missing_core=allowed,
                              definite_missing_core=missing, uncertain_core=uncertain,
                              experience=experience, other_failed_checks=other)


def _exp(years, min_years, passed=None):
    return {"applicable": True, "passed": years >= min_years if passed is None else passed,
            "candidate_years": years, "min_years": min_years}


# --- the rule: the examples agreed in the plan (A = 2) -------------------------

@pytest.mark.parametrize("missing, uncertain, expected", [
    (3, 0, OUTCOME_HOLD),     # one definite skill over the limit
    (2, 1, OUTCOME_HOLD),     # the uncertain match takes them over by one
    (3, 1, OUTCOME_REJECT),   # two over the limit
    (1, 1, OUTCOME_PASS),     # the uncertain one doesn't matter
    (4, 0, OUTCOME_REJECT),
])
def test_core_examples_with_two_allowed(missing, uncertain, expected):
    assert _rule(core=8, allowed=2, missing=missing, uncertain=uncertain).outcome == expected


def test_one_core_near_miss_plus_an_experience_near_miss_rejects():
    decision = _rule(core=4, allowed=1, missing=2, experience=_exp(9.2, 10))
    assert decision.outcome == OUTCOME_REJECT and len(decision.near_misses) == 2


def test_another_failed_check_always_rejects_even_with_one_near_miss():
    assert _rule(core=4, allowed=1, missing=2, other=["Education"]).outcome == OUTCOME_REJECT


# --- zero confirmed core skills always rejects, on small JDs too ----------------

def test_one_core_jd_missing_its_only_core_skill_rejects():
    assert _rule(core=1, allowed=0, missing=1).outcome == OUTCOME_REJECT


def test_one_core_jd_with_its_only_core_skill_uncertain_rejects():
    decision = _rule(core=1, allowed=0, uncertain=1)
    assert decision.outcome == OUTCOME_REJECT and "No core skill confirmed" in decision.reject_reasons


def test_one_core_jd_with_it_confirmed_passes():
    assert _rule(core=1, allowed=0, missing=0).outcome == OUTCOME_PASS


def test_two_core_jd_one_confirmed_one_missing_is_hold():
    decision = _rule(core=2, allowed=0, missing=1)
    assert decision.outcome == OUTCOME_HOLD
    assert decision.review_reason.startswith("Needs recruiter review: 1 core skill over the missing-core limit of 0")


def test_two_core_jd_one_confirmed_one_uncertain_is_hold():
    assert _rule(core=2, allowed=0, uncertain=1).outcome == OUTCOME_HOLD


def test_two_core_jd_none_confirmed_rejects():
    assert _rule(core=2, allowed=0, missing=2).outcome == OUTCOME_REJECT
    assert _rule(core=2, allowed=0, missing=1, uncertain=1, confirmed=0).outcome == OUTCOME_REJECT


def test_two_core_jd_one_missing_and_experience_short_rejects():
    assert _rule(core=2, allowed=0, missing=1, experience=_exp(9.5, 10)).outcome == OUTCOME_REJECT


# --- experience tolerance = max(1.0 year, 10% of the minimum) -------------------

@pytest.mark.parametrize("min_years, tolerance", [(5, 1.0), (10, 1.0), (15, 1.5), (20, 2.0)])
def test_experience_tolerance(min_years, tolerance):
    assert experience_near_miss_tolerance(min_years) == pytest.approx(tolerance)


@pytest.mark.parametrize("years, min_years, expected", [
    (9.0, 10, OUTCOME_HOLD),      # exactly 1.0 short
    (8.9, 10, OUTCOME_REJECT),    # 1.1 short
    (18.0, 20, OUTCOME_HOLD),     # 2.0 short = 10%
    (17.9, 20, OUTCOME_REJECT),
    (10.0, 10, OUTCOME_PASS),
])
def test_experience_short_within_tolerance_is_hold_beyond_it_rejects(years, min_years, expected):
    assert _rule(core=4, allowed=1, missing=0, experience=_exp(years, min_years)).outcome == expected


# --- v1 (live decision): through calculate_and_store_score_breakdown ------------

def _v1(core_names, missing_names, *, experience=None, education=None, max_missing=3, pct=25.0):
    service = CandidateScoringService.__new__(CandidateScoringService)
    service.config_repository = None
    service.campaign_candidate_repository = MagicMock()
    cc = SimpleNamespace(id=uuid4(), deterministic_breakdown=None, deterministic_score=None, deterministic_passed=None)
    service.campaign_candidate_repository.get_by_id.return_value = cc
    skills = [{"canonical_skill_id": str(uuid4()), "canonical_name": name, "importance": "core",
               "match_type": "MISSING" if name in missing_names else "EXACT"} for name in core_names]
    matched = len(core_names) - len(missing_names)
    coverage = round(matched / len(core_names) * 100, 2)
    service.build_mandatory_skill_breakdown = lambda *_: {
        "mandatory_skills": skills, "deterministic_score": 50.0,
        "mandatory_coverage_pct": coverage, "core_coverage_pct": coverage}
    service.build_preferred_skill_breakdown = lambda *_: {"preferred_skills": [], "preferred_skill_bonus": 0.0}
    service.compute_technical_score = lambda *_: 50.0
    breakdown = service.calculate_and_store_score_breakdown(
        uuid4(), uuid4(), uuid4(), 40.0, experience_result=experience, education_result=education,
        max_missing_core_skills=max_missing, max_missing_core_pct=pct)
    return breakdown, cc


FOUR_CORE = ["AI Transformation", "Program Management", "AI Governance", "Change Management"]


def test_v1_one_core_over_the_limit_is_let_through_flagged_for_review():
    breakdown, cc = _v1(FOUR_CORE, ["AI Transformation", "Change Management"])   # 2 missing, 1 allowed
    assert breakdown["deterministic_passed"] is True and cc.deterministic_passed is True
    assert breakdown["needs_review"] is True and breakdown["near_miss_outcome"] == OUTCOME_HOLD
    assert "missing: AI Transformation, Change Management" in breakdown["needs_review_reason"]
    assert needs_recruiter_review(cc) is True


def test_v1_two_over_the_limit_still_rejects():
    breakdown, cc = _v1(FOUR_CORE, FOUR_CORE[:3])
    assert breakdown["deterministic_passed"] is False and breakdown["needs_review"] is False
    assert needs_recruiter_review(cc) is False


def test_v1_clean_pass_has_no_review_flag():
    breakdown, _ = _v1(FOUR_CORE, ["Change Management"])
    assert breakdown["deterministic_passed"] is True and breakdown["needs_review"] is False


def test_v1_experience_within_tolerance_is_review_beyond_it_rejects():
    within = {"applicable": True, "passed": False, "score": 90.0, "candidate_years": 9.0, "min_years": 10.0}
    beyond = {"applicable": True, "passed": False, "score": 87.0, "candidate_years": 8.7, "min_years": 10.0}
    assert _v1(FOUR_CORE, [], experience=within)[0]["needs_review"] is True
    breakdown, _ = _v1(FOUR_CORE, [], experience=beyond)
    assert breakdown["deterministic_passed"] is False and breakdown["needs_review"] is False


def test_v1_one_core_jd_missing_it_rejects():
    breakdown, _ = _v1(["Python"], ["Python"])
    assert breakdown["deterministic_passed"] is False


def test_v1_two_core_jd_one_missing_is_review():
    breakdown, _ = _v1(["Python", "AWS"], ["AWS"])                # 25% of 2 rounds down: 0 allowed
    assert breakdown["deterministic_passed"] is True and breakdown["needs_review"] is True


def test_v1_failed_education_rejects_a_near_miss():
    education = {"applicable": True, "passed": False, "score": 0.0}
    breakdown, _ = _v1(FOUR_CORE, ["AI Transformation", "Change Management"], education=education)
    assert breakdown["deterministic_passed"] is False


# --- v2 ------------------------------------------------------------------------

def test_v2_review_flags_are_carried_when_v2_decides():
    import app.tasks.deterministic_scoring_tasks as task
    cc = SimpleNamespace(deterministic_breakdown=None, deterministic_score=None, deterministic_passed=None)
    task._act_on_v2(cc, {"deterministic_score": 20.0, "deterministic_passed": False, "mandatory_skills": []},
                    {"deterministic_score": 42.0, "deterministic_passed": True, "needs_review": True,
                     "needs_review_reason": "Needs recruiter review: x", "near_misses": ["x"],
                     "near_miss_outcome": OUTCOME_HOLD, "near_miss_reject_reasons": []})
    assert cc.deterministic_passed is True and needs_recruiter_review(cc) is True


def test_v1_result_row_records_hold():
    from app.services.campaign.scoring_v2_service import v1_result_row
    campaign = SimpleNamespace(deterministic_threshold=40, required_skill_coverage_threshold=0)
    row = v1_result_row(campaign, SimpleNamespace(id=uuid4()),
                        {"deterministic_score": 30.0, "deterministic_passed": True, "needs_review": True},
                        acted_on=True, score_weights=None)
    assert row.outcome == "HOLD"


# --- AI step: capped at HOLD; an AI rejection still rejects --------------------

def _ai(recommendation, needs_review):
    from app.tasks.ai_evaluation_tasks import _apply_ai_recommendation
    stage_service = MagicMock()
    cc = SimpleNamespace(deterministic_breakdown={
        "needs_review": needs_review, "needs_review_reason": "Needs recruiter review: 1 core skill over the limit",
        "near_misses": ["1 core skill over the limit"]})
    response = {"recommendation": recommendation, "gaps": ["x"], "scores": {"overall_score": 70}}
    with patch("app.tasks.ai_evaluation_tasks.AIEvaluationService.build_rejection_reason", return_value="gaps"):
        _apply_ai_recommendation(stage_service, cc, recommendation, response)
    return stage_service


@pytest.mark.parametrize("recommendation", ["SHORTLIST", "HOLD"])
def test_near_miss_is_capped_at_hold_whatever_the_ai_recommends(recommendation):
    stage_service = _ai(recommendation, needs_review=True)
    kwargs = stage_service.transition_on_ai_success.call_args.kwargs
    assert kwargs["to_stage"] == PipelineStage.HOLD and kwargs["decision_type"] == DecisionType.HOLD
    assert kwargs["decision_reason"].startswith("Needs recruiter review")
    assert kwargs["decision_source"] == DecisionSource.DETERMINISTIC
    assert kwargs["decision_details"]["capped_at_hold"] is True
    assert kwargs["decision_details"]["ai_recommendation"] == recommendation
    stage_service.transition_to_rejected.assert_not_called()


def test_ai_rejection_still_rejects_a_near_miss():
    stage_service = _ai("REJECT", needs_review=True)
    stage_service.transition_to_rejected.assert_called_once()
    stage_service.transition_on_ai_success.assert_not_called()


def test_without_review_flag_the_ai_shortlist_is_unchanged():
    stage_service = _ai("SHORTLIST", needs_review=False)
    kwargs = stage_service.transition_on_ai_success.call_args.kwargs
    assert kwargs["to_stage"] == PipelineStage.SHORTLISTED and "decision_source" not in kwargs


def test_capped_hold_records_the_given_decision_source():
    from app.services.campaign.stage_transition_service import StageTransitionService
    service = StageTransitionService.__new__(StageTransitionService)
    service.allowed_transition_repo = MagicMock()
    service.allowed_transition_repo.is_transition_allowed.return_value = True
    service.campaign_candidate_repo = MagicMock()
    cc = SimpleNamespace(id=uuid4(), pipeline_stage=PipelineStage.SCREENING, previous_stage=None)
    assert service.transition_on_ai_success(
        cc, to_stage=PipelineStage.HOLD, decision_type=DecisionType.HOLD, change_reason="capped",
        scores_snapshot={}, decision_reason="Needs recruiter review: x", decision_source=DecisionSource.DETERMINISTIC)
    assert cc.pipeline_stage == PipelineStage.HOLD and cc.decision_source == DecisionSource.DETERMINISTIC
    snapshot = service.campaign_candidate_repo.create_stage_history.call_args.kwargs["scores_snapshot"]
    assert snapshot["decision_source"] == "DETERMINISTIC"


def test_nothing_in_the_review_path_emails(no_email):
    _ai("SHORTLIST", needs_review=True)
    _v1(FOUR_CORE, ["AI Transformation", "Change Management"])
    no_email.assert_not_called()
