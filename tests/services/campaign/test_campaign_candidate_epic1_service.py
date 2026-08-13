"""
Epic 1 (Pipeline Stage Management) - integration coverage for the 3 new
CampaignCandidateService methods (advance_to_interview/select_candidate/
reject_at_interview). Exercises the real StageTransitionService (not
mocked) wired into CampaignCandidateService, matching this suite's existing
"integration test" convention (real collaborators, only the DB-facing
repositories mocked) - see test_campaign_candidate_override_integration.py.

allowed_transitions rows used here are the real M12 rows
(app/seeds/seed_allowed_transitions.py), not invented ones:
  HM_REVIEW -> INTERVIEW: [HIRING_MANAGER, HR_ADMIN], reason not required
  INTERVIEW -> SELECTED:  [HIRING_MANAGER, HR_ADMIN], reason not required
  INTERVIEW -> REJECTED:  [HIRING_MANAGER],           reason required
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.schemas.campaign.campaign_candidate_schema import CandidateScorecardResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.stage_transition_service import StageTransitionService


def _make_campaign_candidate(pipeline_stage, campaign_id=None):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=campaign_id or uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        decision_type=None,
        decision_source=None,
        decision_reason=None,
        decision_details=None,
        decision_at=None,
        deterministic_score=None,
        semantic_score=None,
        composite_score=None,
        deterministic_breakdown=None,
        created_at=datetime.now(timezone.utc),
    )


def _make_campaign(hiring_manager_id, campaign_id=None):
    return SimpleNamespace(id=campaign_id or uuid4(), hiring_manager_id=hiring_manager_id)


def _allowed_row(allowed_roles, requires_reason=False):
    return SimpleNamespace(allowed_roles=allowed_roles, requires_reason=requires_reason)


_REAL_ROWS = {
    (PipelineStage.HM_REVIEW, PipelineStage.INTERVIEW): _allowed_row(["HIRING_MANAGER", "HR_ADMIN"]),
    (PipelineStage.INTERVIEW, PipelineStage.SELECTED): _allowed_row(["HIRING_MANAGER", "HR_ADMIN"]),
    (PipelineStage.INTERVIEW, PipelineStage.REJECTED): _allowed_row(["HIRING_MANAGER"], requires_reason=True),
}


def _make_service(campaign_candidate, campaign, allowed_rows=None):
    allowed_rows = _REAL_ROWS if allowed_rows is None else allowed_rows

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate
    campaign_candidate_repo.get_by_id_for_update.return_value = campaign_candidate
    campaign_candidate_repo.create_stage_history_idempotent.return_value = (SimpleNamespace(id=uuid4()), True)

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign

    allowed_transition_repo = MagicMock()
    allowed_transition_repo.get.side_effect = (
        lambda from_stage, to_stage: allowed_rows.get((from_stage, to_stage))
    )

    audit_service = MagicMock()
    stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo, audit_service)

    service = CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        stage_transition_service=stage_transition_service,
    )
    return service, campaign_candidate_repo, campaign_repo, audit_service


# ----------------------------------------------------------------------
# Happy path x3 - writes stage_history + audit_log, returns the scorecard
# with the updated pipeline_stage.
# ----------------------------------------------------------------------

def test_advance_to_interview_happy_path():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    candidate = _make_campaign_candidate(PipelineStage.HM_REVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    result = service.advance_to_interview(candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert isinstance(result, CandidateScorecardResponse)
    assert result.pipeline_stage == PipelineStage.INTERVIEW
    assert candidate.pipeline_stage == PipelineStage.INTERVIEW

    campaign_candidate_repo.create_stage_history_idempotent.assert_called_once()
    history_kwargs = campaign_candidate_repo.create_stage_history_idempotent.call_args.kwargs
    assert history_kwargs["from_stage"] == PipelineStage.HM_REVIEW
    assert history_kwargs["to_stage"] == PipelineStage.INTERVIEW
    assert history_kwargs["changed_by"] == "hm-1"

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] == "hm-1"
    assert audit_service.log.call_args.kwargs["actor_role"] == "HIRING_MANAGER"


def test_select_candidate_happy_path():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    result = service.select_candidate(candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert isinstance(result, CandidateScorecardResponse)
    assert result.pipeline_stage == PipelineStage.SELECTED
    assert candidate.pipeline_stage == PipelineStage.SELECTED

    campaign_candidate_repo.create_stage_history_idempotent.assert_called_once()
    history_kwargs = campaign_candidate_repo.create_stage_history_idempotent.call_args.kwargs
    assert history_kwargs["from_stage"] == PipelineStage.INTERVIEW
    assert history_kwargs["to_stage"] == PipelineStage.SELECTED

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_role"] == "HIRING_MANAGER"


def test_reject_at_interview_happy_path():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    result = service.reject_at_interview(
        candidate.id, decision_reason="Not a fit after the interview.", actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
    )

    assert isinstance(result, CandidateScorecardResponse)
    assert result.pipeline_stage == PipelineStage.REJECTED
    assert candidate.pipeline_stage == PipelineStage.REJECTED

    campaign_candidate_repo.create_stage_history_idempotent.assert_called_once()
    history_kwargs = campaign_candidate_repo.create_stage_history_idempotent.call_args.kwargs
    assert history_kwargs["from_stage"] == PipelineStage.INTERVIEW
    assert history_kwargs["to_stage"] == PipelineStage.REJECTED
    assert history_kwargs["change_reason"] == "Not a fit after the interview."

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_role"] == "HIRING_MANAGER"


# ----------------------------------------------------------------------
# Ownership: HIRING_MANAGER on a candidate outside their own campaign -> 403,
# for all 3 endpoints where an HM can call at all.
# ----------------------------------------------------------------------

def test_advance_to_interview_ownership_403_for_non_owning_hiring_manager():
    campaign = _make_campaign(hiring_manager_id="owner-hm")
    candidate = _make_campaign_candidate(PipelineStage.HM_REVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.advance_to_interview(candidate.id, actor_id="other-hm", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    audit_service.log.assert_not_called()
    assert candidate.pipeline_stage == PipelineStage.HM_REVIEW


def test_select_candidate_ownership_403_for_non_owning_hiring_manager():
    campaign = _make_campaign(hiring_manager_id="owner-hm")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.select_candidate(candidate.id, actor_id="other-hm", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    audit_service.log.assert_not_called()
    assert candidate.pipeline_stage == PipelineStage.INTERVIEW


def test_reject_at_interview_ownership_403_for_non_owning_hiring_manager():
    campaign = _make_campaign(hiring_manager_id="owner-hm")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.reject_at_interview(
            candidate.id, decision_reason="Not a fit.", actor_id="other-hm", actor_roles=["HIRING_MANAGER"],
        )

    assert exc_info.value.status_code == 403
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    audit_service.log.assert_not_called()
    assert candidate.pipeline_stage == PipelineStage.INTERVIEW


# ----------------------------------------------------------------------
# HR_ADMIN on advance-to-interview/select -> succeeds for any candidate, no
# ownership restriction (campaign_repo.get_by_id is never even called).
# ----------------------------------------------------------------------

def test_advance_to_interview_hr_admin_succeeds_without_ownership_check():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    candidate = _make_campaign_candidate(PipelineStage.HM_REVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    result = service.advance_to_interview(candidate.id, actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.pipeline_stage == PipelineStage.INTERVIEW
    campaign_repo.get_by_id.assert_not_called()
    assert audit_service.log.call_args.kwargs["actor_role"] == "HR_ADMIN"


def test_select_candidate_hr_admin_succeeds_without_ownership_check():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    result = service.select_candidate(candidate.id, actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.pipeline_stage == PipelineStage.SELECTED
    campaign_repo.get_by_id.assert_not_called()
    assert audit_service.log.call_args.kwargs["actor_role"] == "HR_ADMIN"


# ----------------------------------------------------------------------
# HR_ADMIN on reject-interview -> 403, and specifically because
# StageTransitionService.transition() raised ForbiddenPipelineRoleException
# (allowed_transitions lists HIRING_MANAGER only for this edge) - not
# because of a route-level or ownership-level check. HR_ADMIN also skips
# the ownership check entirely (campaign_repo.get_by_id never called), so
# this failure is proven to originate purely from transition()'s own role
# check.
# ----------------------------------------------------------------------

def test_reject_at_interview_hr_admin_forbidden_by_transitions_own_role_check():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.reject_at_interview(
            candidate.id, decision_reason="Not a fit.", actor_id="hr-1", actor_roles=["HR_ADMIN"],
        )

    assert exc_info.value.status_code == 403
    assert str(exc_info.value).startswith("FORBIDDEN_ROLE:")
    campaign_repo.get_by_id.assert_not_called()
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    audit_service.log.assert_not_called()
    assert candidate.pipeline_stage == PipelineStage.INTERVIEW


# ----------------------------------------------------------------------
# Wrong-stage attempts: no matching allowed_transitions row for the
# candidate's actual current stage -> InvalidPipelineTransitionException
# from transition(), correctly propagated through _transition_or_raise's
# try/except as a 409 CampaignException.
# ----------------------------------------------------------------------

def test_select_candidate_wrong_stage_propagates_invalid_transition_as_409():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    # Still in HM_REVIEW, not INTERVIEW - no (HM_REVIEW, SELECTED) row exists.
    candidate = _make_campaign_candidate(PipelineStage.HM_REVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.select_candidate(candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 409
    assert str(exc_info.value).startswith("INVALID_TRANSITION:")
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    audit_service.log.assert_not_called()
    assert candidate.pipeline_stage == PipelineStage.HM_REVIEW


def test_advance_to_interview_wrong_stage_propagates_invalid_transition_as_409():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    # Already past HM_REVIEW - no (INTERVIEW, INTERVIEW) row exists either way.
    candidate = _make_campaign_candidate(PipelineStage.INTERVIEW, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.advance_to_interview(candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 409
    assert str(exc_info.value).startswith("INVALID_TRANSITION:")
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()


def test_reject_at_interview_wrong_stage_propagates_invalid_transition_as_409():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    # Not yet in INTERVIEW - no (SHORTLISTED, REJECTED) row in this test's map.
    candidate = _make_campaign_candidate(PipelineStage.SHORTLISTED, campaign_id=campaign.id)
    service, campaign_candidate_repo, campaign_repo, audit_service = _make_service(candidate, campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.reject_at_interview(
            candidate.id, decision_reason="Not a fit.", actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        )

    assert exc_info.value.status_code == 409
    assert str(exc_info.value).startswith("INVALID_TRANSITION:")
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()


# ----------------------------------------------------------------------
# Not found - existence checked before ownership/role, for all 3 methods.
# ----------------------------------------------------------------------

def test_advance_to_interview_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
        stage_transition_service=MagicMock(),
    )

    with pytest.raises(CampaignException) as exc_info:
        service.advance_to_interview(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404
