"""Recruiter overrides: what counts, the reason-code requirement, the row recorded, and every path that checks it."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage as S, TransitionSource
from app.services.campaign import outcome_overrides as oo

MODULE = "app.services.campaign.outcome_overrides"


@pytest.mark.parametrize("outcome, from_stage, to_stage, expected", [
    ("PASS", S.SHORTLISTED, S.REJECTED, ("OVERRIDE", "DOWN")),
    ("PASS", S.SHORTLISTED, S.HOLD, ("OVERRIDE", "DOWN")),
    ("PASS", S.SHORTLISTED, S.HM_REVIEW, None),                     # agrees
    ("REJECT", S.REJECTED, S.SHORTLISTED, ("OVERRIDE", "UP")),
    ("REJECT", S.REJECTED, S.SCREENING, ("OVERRIDE", "UP")),          # the HR override: re-evaluate
    ("REJECT", S.REJECTED, S.HOLD, ("OVERRIDE", "UP")),
    ("REVIEW", S.HOLD, S.HM_REVIEW, ("REVIEW_DECISION", "UP")),       # REVIEW is HOLD
    ("HOLD", S.HOLD, S.REJECTED, ("REVIEW_DECISION", "DOWN")),
    ("HOLD", S.HOLD, S.SCREENING, None),                              # re-screening a HOLD is neutral
    ("PASS", S.HM_REVIEW, S.REJECTED, None),                          # after HM review: a hiring decision
    ("REJECT", S.REJECTED, S.FRAUD_REVIEW, None),
    ("PENDING", S.SCREENING, S.REJECTED, None),
])
def test_what_counts_as_an_override_or_a_review_decision(outcome, from_stage, to_stage, expected):
    assert oo.classify(outcome, from_stage, to_stage) == expected


def _result(outcome="PASS", breakdown=None):
    return SimpleNamespace(id=uuid4(), outcome=outcome, scoring_version="v2", scoring_config_version="cfg",
                           breakdown=breakdown or {})


CC = SimpleNamespace(id=uuid4(), campaign_id=uuid4())


def test_no_person_or_no_decided_outcome_or_an_agreeing_move_records_nothing():
    with patch(f"{MODULE}.acted_on_result", return_value=_result("PASS")):
        assert oo.check_move(None, CC, S.SHORTLISTED, S.REJECTED, actor_id=None, actor_role=None,
                             reason_code=None) is None
        assert oo.check_move(None, CC, S.SHORTLISTED, S.HM_REVIEW, actor_id="r1", actor_role="RECRUITER",
                             reason_code=None) is None
    with patch(f"{MODULE}.acted_on_result", return_value=None):
        assert oo.check_move(None, CC, S.SHORTLISTED, S.REJECTED, actor_id="r1", actor_role="RECRUITER",
                             reason_code=None) is None


def test_a_move_against_the_outcome_without_a_code_is_refused_with_the_codes():
    with patch(f"{MODULE}.acted_on_result", return_value=_result("PASS")), pytest.raises(CampaignException) as err:
        oo.check_move(None, CC, S.SHORTLISTED, S.REJECTED, actor_id="r1", actor_role="RECRUITER", reason_code=None,
                      candidates_in_batch=3)
    assert err.value.status_code == 422
    data = err.value.data
    assert data["code"] == oo.REQUIRED_CODE and data["kind"] == "OVERRIDE" and data["system_outcome"] == "PASS"
    assert data["candidates_in_batch"] == 3
    assert {"code": "JD_CALIBRATION_WRONG", "label": "JD calibration wrong"} in data["reason_codes"]


def test_an_unknown_code_is_refused():
    with patch(f"{MODULE}.acted_on_result", return_value=_result("REJECT")), pytest.raises(CampaignException) as err:
        oo.check_move(None, CC, S.REJECTED, S.SHORTLISTED, actor_id="r1", actor_role="RECRUITER",
                      reason_code="BECAUSE")
    assert err.value.data["code"] == oo.UNKNOWN_CODE


def test_the_row_records_the_contradicted_result_with_codes_and_skill_names_only():
    sid, other = str(uuid4()), str(uuid4())
    breakdown = {"failed_checks": ["Core skills"], "profile_template": {"key": "EXPERIENCED"},
                 "confirmation": {"results": [{"canonical_skill_id": sid, "name": "MLOps", "status": "UNCERTAIN"},
                                              {"canonical_skill_id": other, "name": "Python", "status": "CONFIRMED"},
                                              {"canonical_skill_id": str(uuid4()), "name": "SQL",
                                               "status": "NOT_FOUND"}]},
                 "paraphrase": {"results": [{"canonical_skill_id": sid, "status": "CONFIRMED"}]}}
    result = _result("REJECT", breakdown)
    with patch(f"{MODULE}.acted_on_result", return_value=result):
        row = oo.check_move(None, CC, S.REJECTED, S.SCREENING, actor_id="r1", actor_role="RECRUITER",
                            reason_code="SKILL_EVIDENCE_MISSED")
    assert (row.kind, row.direction, row.system_outcome, row.reason_code) == ("OVERRIDE", "UP", "REJECT",
                                                                             "SKILL_EVIDENCE_MISSED")
    assert row.scoring_result_id == result.id and row.campaign_id == CC.campaign_id
    assert row.failed_checks == ["Core skills"] and row.profile_template == "EXPERIENCED"
    assert row.missing_core_skills == ["SQL"]                  # MLOps was confirmed by the paraphrase round
    assert (row.from_stage, row.to_stage, row.actor_id, row.actor_role) == (S.REJECTED, S.SCREENING, "r1", "RECRUITER")


# --- every path a person moves a candidate by ------------------------------------------------

def _pipeline_service():
    from app.services.campaign.pipeline_transition_service import PipelineTransitionService
    service = PipelineTransitionService.__new__(PipelineTransitionService)
    service.allowed_transition_repo = MagicMock()
    service.allowed_transition_repo.get.return_value = SimpleNamespace(allowed_roles=["RECRUITER", "SYSTEM"],
                                                                      requires_reason=False)
    service.campaign_candidate_repo = MagicMock()
    service.interview_schedule_repo = MagicMock()
    service.audit_service = MagicMock()
    return service


@pytest.mark.parametrize("source, expected_actor", [(TransitionSource.MANUAL, "r1"), (TransitionSource.SYSTEM, None)])
def test_the_transition_engine_checks_a_persons_move_and_records_the_override(source, expected_actor):
    service = _pipeline_service()
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), pipeline_stage=S.SHORTLISTED, previous_stage=S.SCREENING)
    marker = object()

    class Stop(BaseException):
        pass

    def check(db, candidate, from_stage, to_stage, **kwargs):
        assert (from_stage, to_stage, kwargs["actor_id"], kwargs["reason_code"]) == (
            S.SHORTLISTED, S.REJECTED, expected_actor, "ROLE_FIT")
        return marker

    service.campaign_candidate_repo.update_pipeline_stage.side_effect = Stop        # stop after the check
    with patch("app.services.campaign.pipeline_transition_service.check_move", side_effect=check), \
            pytest.raises(Stop):
        service.transition_stage(cc, S.REJECTED, changed_by="r1", actor_role="RECRUITER", source=source,
                                 override_reason_code="ROLE_FIT")
    service.campaign_candidate_repo.db.add.assert_called_once_with(marker)


def test_a_refused_override_changes_nothing():
    service = _pipeline_service()
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), pipeline_stage=S.SHORTLISTED, previous_stage=None)
    with patch("app.services.campaign.pipeline_transition_service.check_move",
               side_effect=CampaignException("choose a reason", 422)), pytest.raises(CampaignException):
        service.transition_stage(cc, S.REJECTED, changed_by="r1", actor_role="RECRUITER",
                                 source=TransitionSource.MANUAL)
    service.campaign_candidate_repo.update_pipeline_stage.assert_not_called()
    assert cc.pipeline_stage == S.SHORTLISTED


def test_bulk_single_and_reject_moves_pass_the_code_on():
    from app.services.campaign.bulk_stage_move_service import BulkStageMoveService
    service = BulkStageMoveService.__new__(BulkStageMoveService)
    service.campaign_candidate_repo = MagicMock()
    service.pipeline_transition_service = MagicMock()
    service.audit_service = MagicMock()
    service.notifier = MagicMock()
    campaign_id = uuid4()
    ccs = [SimpleNamespace(id=uuid4(), campaign_id=campaign_id, pipeline_stage=S.SHORTLISTED) for _ in range(2)]
    service.campaign_candidate_repo.get_by_id.side_effect = lambda i: next(c for c in ccs if c.id == i)
    service.bulk_move(campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in ccs], target_stage="REJECTED",
                      reason="Two candidates rejected after review", actor_id="hr", actor_role="HR_ADMIN",
                      override_reason_code="ROLE_FIT")
    kwargs = service.pipeline_transition_service.transition_stage.call_args.kwargs
    assert kwargs["override_reason_code"] == "ROLE_FIT" and kwargs["candidates_in_batch"] == 2
    service.reject_one(campaign_id=campaign_id, campaign_candidate_id=ccs[0].id, reason="Not a fit for the role",
                       actor_id="r1", actor_role="RECRUITER", override_reason_code="EXPERIENCE_JUDGEMENT")
    assert service.pipeline_transition_service.transition_stage.call_args.kwargs["override_reason_code"] == \
        "EXPERIENCE_JUDGEMENT"


def test_the_hr_override_and_the_stalled_override_check_and_record():
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    from app.models.pipeline import DecisionSource, DecisionType
    marker = object()
    service = CampaignCandidateService.__new__(CampaignCandidateService)
    service.campaign_candidate_repo = MagicMock()
    service.ai_evaluation_repo = MagicMock()
    service.stage_transition_service = MagicMock()
    service.stage_transition_service.apply_hr_override.return_value = False      # stop right after the check
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), pipeline_stage=S.REJECTED,
                         decision_type=DecisionType.REJECTED, decision_source=DecisionSource.DETERMINISTIC)
    service.campaign_candidate_repo.get_by_id.return_value = cc
    with patch("app.services.campaign.campaign_candidate_service.check_move", return_value=marker) as check, \
            pytest.raises(CampaignException):
        service.apply_hr_override(cc.id, override_reason="The skills are there in the projects section",
                                  actor_id="r1", actor_role="RECRUITER", override_reason_code="SKILL_EVIDENCE_MISSED")
    args, kwargs = check.call_args
    assert args[2:] == (S.REJECTED, S.SCREENING) and kwargs["reason_code"] == "SKILL_EVIDENCE_MISSED"
    service.campaign_candidate_repo.db.add.assert_not_called()                  # the transition failed: not recorded

    from app.schemas.campaign.campaign_monitoring_schema import StageOverrideRequest
    from app.services.campaign.campaign_service import CampaignService
    campaign_service = CampaignService.__new__(CampaignService)
    campaign_service.campaign_repo = MagicMock()
    stalled = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), pipeline_stage=S.HOLD)

    class Stop(BaseException):
        pass

    campaign_service._get_stalled_candidate_or_raise = lambda *a: stalled
    campaign_service.campaign_repo.transition_candidate_stage.side_effect = Stop
    with patch("app.services.campaign.campaign_service.check_move", return_value=marker) as check, pytest.raises(Stop):
        campaign_service.override_candidate_stage(
            stalled.campaign_id, stalled.id, StageOverrideRequest(reason="Reviewed", override_reason_code="ROLE_FIT"),
            actor_id="hr", actor_role="HR_ADMIN")
    args, kwargs = check.call_args
    assert args[2:] == (S.HOLD, S.HM_REVIEW) and kwargs["reason_code"] == "ROLE_FIT"
