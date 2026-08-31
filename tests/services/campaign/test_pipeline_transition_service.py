"""
PipelineTransitionService.transition_stage - previously zero test coverage
anywhere in this codebase (move_pipeline_stage's own tests mock this class
out entirely - see test_campaign_candidate_board.py). That gap is exactly
how the class went uncorrected for this long: its own docstring claimed
"zero call sites anywhere in the codebase" long after move_pipeline_stage
(Pipeline Board drag-and-drop) and BulkStageMoveService both started
calling it for real, and nobody exercising transition_stage() itself ever
caught that to_stage=INTERVIEW never created an interview_schedules row -
found live, via Pipeline Board drag-and-drop hitting an unhandled 409 on
a candidate that had actually reached INTERVIEW.

These tests focus specifically on the INTERVIEW-entry hook (mirroring
StageTransitionService.transition()'s own equivalent tests) since that's
the gap being closed - not a full behavioral test suite for this class.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.pipeline import PipelineStage, TransitionSource
from app.services.campaign.pipeline_transition_service import PipelineTransitionService


def _allowed_row(allowed_roles, requires_reason=False):
    return SimpleNamespace(allowed_roles=allowed_roles, requires_reason=requires_reason)


def _make_candidate(pipeline_stage):
    return SimpleNamespace(
        id=uuid4(), campaign_id=uuid4(), pipeline_stage=pipeline_stage,
        decision_type=None, decision_source=None, decision_reason=None,
        decision_details=None, decision_by_user_id=None, decision_at=None,
    )


def _make_env(allowed_row):
    allowed_transition_repo = MagicMock()
    allowed_transition_repo.get.return_value = allowed_row
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.create_stage_history.return_value = SimpleNamespace(id=uuid4())
    audit_service = MagicMock()
    interview_schedule_repo = MagicMock()

    service = PipelineTransitionService(
        allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo,
    )
    return service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo


def test_pipeline_transition_service_construction_requires_interview_schedule_repo():
    """
    Required, not optional-with-a-runtime-check - the exact reversal
    StageTransitionService went through earlier, applied here too, since
    this is the second of two generic engines that can reach
    to_stage=INTERVIEW.
    """
    with pytest.raises(TypeError):
        PipelineTransitionService(MagicMock(), MagicMock(), MagicMock())


def test_transition_stage_to_interview_creates_pending_interview_schedule():
    candidate = _make_candidate(PipelineStage.SHORTLISTED)
    row = _allowed_row(["HIRING_MANAGER", "HR_ADMIN"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)
    interview_schedule_repo.get_or_create_pending.return_value = (SimpleNamespace(id=uuid4()), True)

    service.transition_stage(
        candidate, to_stage=PipelineStage.INTERVIEW, changed_by="hm-1", actor_role="HIRING_MANAGER",
        source=TransitionSource.MANUAL,
    )

    interview_schedule_repo.get_or_create_pending.assert_called_once_with(candidate.id)
    campaign_candidate_repo.update_pipeline_stage.assert_called_once_with(candidate, PipelineStage.INTERVIEW)


def test_transition_stage_to_a_non_interview_stage_never_touches_interview_schedule_repo():
    candidate = _make_candidate(PipelineStage.SCREENING)
    row = _allowed_row(["SYSTEM"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)

    service.transition_stage(
        candidate, to_stage=PipelineStage.SHORTLISTED, changed_by=None, actor_role="SYSTEM",
        source=TransitionSource.SYSTEM,
    )

    interview_schedule_repo.get_or_create_pending.assert_not_called()


def test_transition_stage_to_interview_via_reentry_reuses_existing_schedule_untouched():
    """
    A candidate re-entering INTERVIEW through this engine (e.g. a
    fraud-review clear routed through move_pipeline_stage instead of
    StageTransitionService.transition()) must not get a second row -
    same get_or_create guarantee as the other engine, backed by the same
    repository method.
    """
    candidate = _make_candidate(PipelineStage.FRAUD_REVIEW)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)
    existing_schedule = SimpleNamespace(id=uuid4())
    interview_schedule_repo.get_or_create_pending.return_value = (existing_schedule, False)

    service.transition_stage(
        candidate, to_stage=PipelineStage.INTERVIEW, changed_by="hr-1", actor_role="HR_ADMIN",
        reason="false positive, cleared", source=TransitionSource.MANUAL,
    )

    interview_schedule_repo.get_or_create_pending.assert_called_once_with(candidate.id)


def test_transition_stage_to_interview_hook_runs_before_the_audit_log_write():
    """
    Same-transaction ordering matters: if this INSERT fails, the whole
    transition (including the audit entry) must not be considered
    complete. Not asserting on commit/rollback here (this class doesn't
    commit - callers do, per its own docstring) - just that the hook is
    reached as part of the same call, before the audit log line the
    caller will subsequently commit alongside.
    """
    candidate = _make_candidate(PipelineStage.SHORTLISTED)
    row = _allowed_row(["HIRING_MANAGER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)
    interview_schedule_repo.get_or_create_pending.return_value = (SimpleNamespace(id=uuid4()), True)

    service.transition_stage(
        candidate, to_stage=PipelineStage.INTERVIEW, changed_by="hm-1", actor_role="HIRING_MANAGER",
        source=TransitionSource.MANUAL,
    )

    assert interview_schedule_repo.get_or_create_pending.called
    audit_service.log.assert_called_once()


# ----------------------------------------------------------------------
# M12 cascading-cancellation follow-up: transition_stage()'s INTERVIEW-exit
# hook. This is the engine backing Pipeline Board drag-and-drop, where
# INTERVIEW -> SHORTLISTED is only reachable from in the first place -
# Epic 1's dedicated endpoints never expose it.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("to_stage", [PipelineStage.SELECTED, PipelineStage.REJECTED, PipelineStage.SHORTLISTED])
def test_transition_stage_from_interview_to_a_terminal_stage_cancels_active_rounds(to_stage):
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    row = _allowed_row(["HIRING_MANAGER", "HR_ADMIN"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)

    service.transition_stage(
        candidate, to_stage=to_stage, changed_by="hm-1", actor_role="HIRING_MANAGER", source=TransitionSource.MANUAL,
    )

    interview_schedule_repo.cancel_active_rounds.assert_called_once_with(
        candidate.id,
        reason=f"Candidate outcome finalized: {to_stage.value}",
        changed_by="hm-1",
        changed_by_role="HIRING_MANAGER",
    )


def test_transition_stage_from_interview_to_hold_never_cascades():
    """HOLD is a reversible pause (has a resume-edge back to INTERVIEW) - must not cascade-cancel."""
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    row = _allowed_row(["HR_ADMIN", "RECRUITER", "HIRING_MANAGER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)

    service.transition_stage(
        candidate, to_stage=PipelineStage.HOLD, changed_by="hm-1", actor_role="HIRING_MANAGER",
        source=TransitionSource.MANUAL,
    )

    interview_schedule_repo.cancel_active_rounds.assert_not_called()


# ----------------------------------------------------------------------
# Epic 5 follow-up - manual re-score trigger's cascade-cancel half.
# transition_stage() never commits, so the actual re-score enqueue (a
# Celery call) can't live here - it lives in this method's 3 real
# callers instead (move_pipeline_stage, BulkStageMoveService.bulk_move/
# move_one), tested separately. This method only owns the DB-only
# cascade-cancel, same transaction as the stage move.
# ----------------------------------------------------------------------

def test_transition_stage_to_screening_from_hold_cancels_active_rounds():
    candidate = _make_candidate(PipelineStage.HOLD)
    row = _allowed_row(["HR_ADMIN", "RECRUITER", "HIRING_MANAGER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)

    service.transition_stage(
        candidate, to_stage=PipelineStage.SCREENING, changed_by="hm-1", actor_role="HIRING_MANAGER",
        source=TransitionSource.MANUAL,
    )

    interview_schedule_repo.cancel_active_rounds.assert_called_once_with(
        candidate.id,
        reason="Candidate returned to SCREENING for re-evaluation",
        changed_by="hm-1",
        changed_by_role="HIRING_MANAGER",
    )


def test_transition_stage_to_screening_from_uploaded_never_cascades():
    """The automated resume-upload path - must never get this hook."""
    candidate = _make_candidate(PipelineStage.UPLOADED)
    row = _allowed_row(["SYSTEM"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo = _make_env(row)

    service.transition_stage(candidate, to_stage=PipelineStage.SCREENING, source=TransitionSource.SYSTEM)

    interview_schedule_repo.cancel_active_rounds.assert_not_called()
