from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.exceptions.pipeline_transition_exceptions import (
    ForbiddenPipelineRoleException,
    InvalidPipelineTransitionException,
    PipelineStageConflictException,
    PipelineTransitionReasonRequiredException,
)
from app.models.pipeline import DecisionSource, PipelineStage, TransitionSource
from app.services.campaign.stage_transition_service import Actor, StageTransitionService

"""
M07-E03 S02 T01: StageTransitionService.transition_to_rejected - validates
against allowed_transitions before ever writing pipeline_stage or
campaign_candidate_stage_history, so a missing/blocked transition is a
clean no-op, never a partial write.
"""


def _make_candidate(pipeline_stage=PipelineStage.SCREENING):
    # decision_* fields default to None here, matching a real CampaignCandidate
    # row that has never had a decision recorded yet - transition_to_rejected/
    # apply_hr_override both read these (apply_hr_override reads them before
    # this fixture existed, to snapshot the decision being overridden).
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        pipeline_stage=pipeline_stage,
        decision_type=None,
        decision_source=None,
        decision_reason=None,
        decision_details=None,
        decision_by_user_id=None,
        decision_at=None,
    )


def make_service(is_allowed: bool):
    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = is_allowed
    campaign_candidate_repo = MagicMock()
    service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo, MagicMock())
    return service, allowed_transition_repo, campaign_candidate_repo


def test_transition_applies_when_allowed():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)
    snapshot = {"deterministic_score": 40.0}

    result = service.transition_to_rejected(
        candidate,
        change_reason="Deterministic filter rejection",
        scores_snapshot=snapshot,
        decision_source=DecisionSource.DETERMINISTIC,
    )

    assert result is True
    assert candidate.pipeline_stage == PipelineStage.REJECTED
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.SCREENING, PipelineStage.REJECTED,
    )
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.SCREENING,
        to_stage=PipelineStage.REJECTED,
        changed_by=None,
        change_reason="Deterministic filter rejection",
        transition_source=TransitionSource.SYSTEM,
        scores_snapshot={
            **snapshot,
            "decision_type": "REJECTED",
            "decision_source": "DETERMINISTIC",
            "decision_reason": "Deterministic filter rejection",
            "decision_details": None,
        },
    )


def test_transition_is_a_no_op_when_blocked():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=False)

    result = service.transition_to_rejected(
        candidate,
        change_reason="Deterministic filter rejection",
        scores_snapshot={},
        decision_source=DecisionSource.DETERMINISTIC,
    )

    assert result is False
    # pipeline_stage must be untouched - still SCREENING, not silently REJECTED.
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()


"""
StageTransitionService.transition_to_screening - moves UPLOADED ->
SCREENING right before deterministic scoring runs, so a rejection
immediately afterwards has a real SCREENING -> REJECTED edge to use.
"""


def test_transition_to_screening_applies_when_uploaded_and_allowed():
    candidate = _make_candidate(pipeline_stage=PipelineStage.UPLOADED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)

    result = service.transition_to_screening(candidate)

    assert result is True
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.UPLOADED, PipelineStage.SCREENING,
    )
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.UPLOADED,
        to_stage=PipelineStage.SCREENING,
        changed_by=None,
        change_reason="Automated screening started",
        transition_source=TransitionSource.SYSTEM,
        scores_snapshot=None,
    )


def test_transition_to_screening_is_a_no_op_when_blocked():
    candidate = _make_candidate(pipeline_stage=PipelineStage.UPLOADED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=False)

    result = service.transition_to_screening(candidate)

    assert result is False
    assert candidate.pipeline_stage == PipelineStage.UPLOADED
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()


def test_transition_to_screening_is_a_no_op_when_not_uploaded():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)

    result = service.transition_to_screening(candidate)

    assert result is False
    # Never re-enters SCREENING or writes a second history row for a
    # candidate that's already there (e.g. a retried scoring task).
    allowed_transition_repo.is_transition_allowed.assert_not_called()
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()


"""
M07-E03 S04 T02: StageTransitionService.apply_hr_override - same
validate-then-apply shape as transition_to_rejected, but REJECTED ->
SCREENING, MANUAL/HR_ADMIN-attributed instead of SYSTEM/anonymous.
"""


def test_apply_hr_override_applies_when_allowed():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)

    result = service.apply_hr_override(
        candidate, changed_by="hr-admin-1", change_reason="HR_ADMIN override of deterministic rejection",
    )

    assert result is True
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.REJECTED, PipelineStage.SCREENING,
    )
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.REJECTED,
        to_stage=PipelineStage.SCREENING,
        changed_by="hr-admin-1",
        change_reason="HR_ADMIN override of deterministic rejection",
        transition_source=TransitionSource.MANUAL,
        scores_snapshot={
            "decision_type": "RESET",
            "decision_source": "HR_ADMIN",
            "decision_reason": "HR_ADMIN override of deterministic rejection",
            "decision_details": {
                "overridden_decision_type": None,
                "overridden_decision_source": None,
                "overridden_decision_reason": None,
                "overridden_decision_at": None,
            },
        },
    )


def test_apply_hr_override_is_a_no_op_when_blocked():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=False)

    result = service.apply_hr_override(
        candidate, changed_by="hr-admin-1", change_reason="HR_ADMIN override of deterministic rejection",
    )

    assert result is False
    # pipeline_stage must be untouched - still REJECTED, not silently SCREENING.
    assert candidate.pipeline_stage == PipelineStage.REJECTED
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()


"""
E02: StageTransitionService.transition() - single validated entry point,
checked in this exact order: existence -> role -> reason -> concurrency
(FOR UPDATE re-check) -> idempotent write -> stage move + audit_log, all in
one uncommitted transaction until the final commit(). Every test below also
asserts whether audit_log did or didn't get written, since that's the one
behavior genuinely new to this method versus transition_to_rejected/
apply_hr_override above (neither of which ever writes audit_log).
"""


def _allowed_row(allowed_roles, requires_reason=False):
    return SimpleNamespace(allowed_roles=allowed_roles, requires_reason=requires_reason)


def _make_transition_env(candidate, allowed_row, locked_candidate=None):
    """
    allowed_transition_repo.get() is a strict single-pair mock by default
    (returns allowed_row only, regardless of args) - tests that need to
    prove two different (from_stage, to_stage) pairs are resolved
    independently override .side_effect themselves instead of relying on
    this default.
    """
    allowed_transition_repo = MagicMock()
    allowed_transition_repo.get.return_value = allowed_row
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    campaign_candidate_repo.get_by_id_for_update.return_value = locked_candidate if locked_candidate is not None else candidate
    campaign_candidate_repo.create_stage_history_idempotent.return_value = (
        SimpleNamespace(id=uuid4()), True,
    )
    audit_service = MagicMock()
    service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo, audit_service)
    return service, allowed_transition_repo, campaign_candidate_repo, audit_service


"""
Both directions of REJECTED<->SCREENING and REJECTED<->SHORTLISTED,
verified against the real allowed_transitions rows (not invented ones) so
this also documents the real graph: REJECTED->SCREENING and
REJECTED->SHORTLISTED both exist ([HR_ADMIN], reason required);
SCREENING->REJECTED exists too but with entirely different rules
([SYSTEM, HR_ADMIN, RECRUITER], no reason required) - proving the two
directions aren't sharing config. SHORTLISTED->REJECTED does NOT exist in
the real graph - included here specifically to prove the reverse of an
allowed edge isn't implicitly allowed just because the forward direction
is.
"""


def test_rejected_to_screening_uses_its_own_role_and_reason_rules():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id, PipelineStage.SCREENING, Actor(roles=["HR_ADMIN"], id="hr-1"), reason="reinstating",
    )

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.get.assert_called_once_with(PipelineStage.REJECTED, PipelineStage.SCREENING)
    audit_service.log.assert_called_once()


def test_screening_to_rejected_uses_its_own_role_and_reason_rules_not_the_reverse_edges():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    # Deliberately the opposite rules from REJECTED->SCREENING above (more
    # roles, no reason required) - if the code were accidentally reusing
    # the reverse edge's row, this would fail requiring a reason.
    row = _allowed_row(["SYSTEM", "HR_ADMIN", "RECRUITER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id, PipelineStage.REJECTED, Actor.system(),
    )

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.REJECTED
    allowed_transition_repo.get.assert_called_once_with(PipelineStage.SCREENING, PipelineStage.REJECTED)
    audit_service.log.assert_called_once()


def test_rejected_to_shortlisted_uses_its_own_role_and_reason_rules():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id, PipelineStage.SHORTLISTED, Actor(roles=["HR_ADMIN"], id="hr-1"), reason="reinstating directly",
    )

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.SHORTLISTED
    allowed_transition_repo.get.assert_called_once_with(PipelineStage.REJECTED, PipelineStage.SHORTLISTED)
    audit_service.log.assert_called_once()


def test_shortlisted_to_rejected_is_not_allowed_even_though_the_reverse_edge_is():
    # No such row in the real allowed_transitions graph - the reverse of an
    # allowed edge is not automatically allowed.
    candidate = _make_candidate(pipeline_stage=PipelineStage.SHORTLISTED)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, None)

    with pytest.raises(InvalidPipelineTransitionException) as exc_info:
        service.transition(candidate.id, PipelineStage.REJECTED, Actor(roles=["HR_ADMIN"], id="hr-1"), reason="x")

    assert str(exc_info.value).startswith("INVALID_TRANSITION:")
    allowed_transition_repo.get.assert_called_once_with(PipelineStage.SHORTLISTED, PipelineStage.REJECTED)
    campaign_candidate_repo.get_by_id_for_update.assert_not_called()
    audit_service.log.assert_not_called()


"""
All 5 *->FRAUD_REVIEW edges: SYSTEM-only in the real graph
(allowed_roles=[SYSTEM], requires_reason=False for every one of the 5).
"""

_FRAUD_REVIEW_SOURCE_STAGES = [
    PipelineStage.UPLOADED, PipelineStage.SCREENING, PipelineStage.SHORTLISTED,
    PipelineStage.HM_REVIEW, PipelineStage.INTERVIEW,
]


@pytest.mark.parametrize("from_stage", _FRAUD_REVIEW_SOURCE_STAGES)
def test_system_actor_can_trigger_every_fraud_review_edge(from_stage):
    candidate = _make_candidate(pipeline_stage=from_stage)
    row = _allowed_row(["SYSTEM"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(candidate.id, PipelineStage.FRAUD_REVIEW, Actor.system())

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.FRAUD_REVIEW
    audit_service.log.assert_called_once()


@pytest.mark.parametrize("from_stage", _FRAUD_REVIEW_SOURCE_STAGES)
@pytest.mark.parametrize("human_role", ["RECRUITER", "HR_ADMIN", "HIRING_MANAGER"])
def test_no_human_role_can_trigger_any_fraud_review_edge(from_stage, human_role):
    candidate = _make_candidate(pipeline_stage=from_stage)
    row = _allowed_row(["SYSTEM"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    with pytest.raises(ForbiddenPipelineRoleException) as exc_info:
        service.transition(candidate.id, PipelineStage.FRAUD_REVIEW, Actor(roles=[human_role], id="u-1"))

    assert str(exc_info.value).startswith("FORBIDDEN_ROLE:")
    campaign_candidate_repo.get_by_id_for_update.assert_not_called()
    audit_service.log.assert_not_called()


"""
All 5 FRAUD_REVIEW->* edges: HR_ADMIN-only in the real graph
(allowed_roles=[HR_ADMIN], requires_reason=True for every one of the 5) -
every other role (explicitly including HIRING_MANAGER, since it's easy to
mistakenly assume a "review" role like HIRING_MANAGER should have fraud
clearance too) is rejected, and HR_ADMIN without a reason is rejected with
PipelineTransitionReasonRequiredException specifically, not silently
allowed through.
"""

_FRAUD_REVIEW_TARGET_STAGES = [
    PipelineStage.SCREENING, PipelineStage.SHORTLISTED, PipelineStage.HM_REVIEW,
    PipelineStage.INTERVIEW, PipelineStage.REJECTED,
]


@pytest.mark.parametrize("to_stage", _FRAUD_REVIEW_TARGET_STAGES)
def test_hr_admin_with_reason_can_clear_every_fraud_review_edge(to_stage):
    candidate = _make_candidate(pipeline_stage=PipelineStage.FRAUD_REVIEW)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id, to_stage, Actor(roles=["HR_ADMIN"], id="hr-1"), reason="false positive, cleared",
    )

    assert was_created is True
    assert result.pipeline_stage == to_stage
    audit_service.log.assert_called_once()


@pytest.mark.parametrize("to_stage", _FRAUD_REVIEW_TARGET_STAGES)
@pytest.mark.parametrize("other_role", ["RECRUITER", "HIRING_MANAGER", "SYSTEM"])
def test_no_other_role_including_hiring_manager_can_clear_any_fraud_review_edge(to_stage, other_role):
    candidate = _make_candidate(pipeline_stage=PipelineStage.FRAUD_REVIEW)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    with pytest.raises(ForbiddenPipelineRoleException) as exc_info:
        service.transition(candidate.id, to_stage, Actor(roles=[other_role], id="u-1"), reason="doesn't matter")

    assert str(exc_info.value).startswith("FORBIDDEN_ROLE:")
    campaign_candidate_repo.get_by_id_for_update.assert_not_called()
    audit_service.log.assert_not_called()


@pytest.mark.parametrize("to_stage", _FRAUD_REVIEW_TARGET_STAGES)
def test_hr_admin_without_reason_cannot_clear_any_fraud_review_edge(to_stage):
    candidate = _make_candidate(pipeline_stage=PipelineStage.FRAUD_REVIEW)
    row = _allowed_row(["HR_ADMIN"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    with pytest.raises(PipelineTransitionReasonRequiredException) as exc_info:
        service.transition(candidate.id, to_stage, Actor(roles=["HR_ADMIN"], id="hr-1"))

    assert str(exc_info.value).startswith("REASON_REQUIRED:")
    campaign_candidate_repo.get_by_id_for_update.assert_not_called()
    audit_service.log.assert_not_called()


"""
A human actor manually forcing one of the SYSTEM-normal pre-shortlist
transitions succeeds - the override door is genuinely open, not just
documented as open. UPLOADED->SCREENING's real allowed_roles is
[SYSTEM, HR_ADMIN, RECRUITER].
"""


def test_recruiter_can_manually_force_uploaded_to_screening():
    candidate = _make_candidate(pipeline_stage=PipelineStage.UPLOADED)
    row = _allowed_row(["SYSTEM", "HR_ADMIN", "RECRUITER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id, PipelineStage.SCREENING, Actor(roles=["RECRUITER"], id="rec-1"),
    )

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.SCREENING
    # MANUAL/actor-attributed, not SYSTEM/anonymous - a human genuinely
    # forced this, and the history row must say so.
    history_call = campaign_candidate_repo.create_stage_history_idempotent.call_args.kwargs
    assert history_call["changed_by"] == "rec-1"
    assert history_call["transition_source"] == TransitionSource.MANUAL
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] == "rec-1"
    assert audit_service.log.call_args.kwargs["actor_role"] == "RECRUITER"


"""
Actor.roles is a list, not a single role (Epic 1 follow-up) - a dual-role
actor succeeds on an edge that only lists ONE of their roles, via that one
matching role, and is not locked out just because their other role isn't
listed. INTERVIEW->REJECTED's real allowed_roles=[HIRING_MANAGER] (HR_ADMIN
has no path to this edge at all) is used here deliberately, not an
invented row. HR_ADMIN is listed first in actor.roles specifically so this
would fail if the resolved-role logic naively picked roles[0] instead of
the first role that's actually a member of allowed_roles.
"""


def test_dual_role_actor_succeeds_via_matching_role_and_audit_logs_that_role():
    candidate = _make_candidate(pipeline_stage=PipelineStage.INTERVIEW)
    row = _allowed_row(["HIRING_MANAGER"], requires_reason=True)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)

    result, was_created = service.transition(
        candidate.id,
        PipelineStage.REJECTED,
        Actor(roles=["HR_ADMIN", "HIRING_MANAGER"], id="hm-1"),
        reason="not a fit after interview",
    )

    assert was_created is True
    assert result.pipeline_stage == PipelineStage.REJECTED
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] == "hm-1"
    assert audit_service.log.call_args.kwargs["actor_role"] == "HIRING_MANAGER"


"""
Concurrent double-submit on the same transition: the first request
succeeds; a second request either (a) loses the race because from_stage
already moved (PipelineStageConflictException) or (b) is a true retry
under the same idempotency_key (idempotent-replay path) - never a
duplicate history row either way.
"""


def test_concurrent_transition_where_stage_already_moved_raises_conflict():
    # The unlocked read (get_by_id) still sees SCREENING - stale, fetched
    # before the other request committed - but the FOR UPDATE lock
    # (get_by_id_for_update) sees the real, already-moved state.
    stale_candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    locked_candidate = _make_candidate(pipeline_stage=PipelineStage.SHORTLISTED)
    row = _allowed_row(["SYSTEM", "HR_ADMIN", "RECRUITER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(
        stale_candidate, row, locked_candidate=locked_candidate,
    )

    with pytest.raises(PipelineStageConflictException) as exc_info:
        service.transition(stale_candidate.id, PipelineStage.REJECTED, Actor.system())

    assert str(exc_info.value).startswith("STAGE_CONFLICT:")
    campaign_candidate_repo.create_stage_history_idempotent.assert_not_called()
    campaign_candidate_repo.update.assert_not_called()
    audit_service.log.assert_not_called()


def test_concurrent_transition_with_same_idempotency_key_replays_without_duplicate_write():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    row = _allowed_row(["SYSTEM", "HR_ADMIN", "RECRUITER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)
    # Simulates the SAVEPOINT + IntegrityError-catch fallback: the insert
    # lost the race against an identical, already-committed request under
    # the same idempotency_key.
    existing_history = SimpleNamespace(id=uuid4())
    campaign_candidate_repo.create_stage_history_idempotent.return_value = (existing_history, False)

    result, was_created = service.transition(
        candidate.id, PipelineStage.REJECTED, Actor.system(), idempotency_key="retry-key-1",
    )

    assert was_created is False
    # Not re-applied: the stage move and audit log only happen when
    # was_created is True.
    campaign_candidate_repo.update.assert_not_called()
    audit_service.log.assert_not_called()
    campaign_candidate_repo.commit.assert_called_once()


"""
A transition request naming a nonexistent (from_stage, to_stage) pair
raises InvalidPipelineTransitionException specifically - not a role or
reason error - confirming existence is checked before either, even when
the actor/reason given would ALSO have failed those later checks.
"""


def test_nonexistent_pair_raises_invalid_transition_not_role_or_reason_error():
    candidate = _make_candidate(pipeline_stage=PipelineStage.UPLOADED)
    # No allowed_transitions row for UPLOADED -> INTERVIEW.
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, None)

    # Actor/reason chosen so that IF role or reason were checked first,
    # this would raise a different exception - it must not.
    with pytest.raises(InvalidPipelineTransitionException) as exc_info:
        service.transition(candidate.id, PipelineStage.INTERVIEW, Actor(roles=["RECRUITER"], id="rec-1"), reason=None)

    assert str(exc_info.value).startswith("INVALID_TRANSITION:")
    allowed_transition_repo.get.assert_called_once_with(PipelineStage.UPLOADED, PipelineStage.INTERVIEW)
    campaign_candidate_repo.get_by_id_for_update.assert_not_called()
    audit_service.log.assert_not_called()


"""
An unexpected error (not one of the 4 defined exceptions) during the
audit_log write. transition() has no local try/except/finally (confirmed
by inspection - see E02 Step 2 report part C), so this must propagate
uncaught, and neither commit() nor rollback() is called by transition()
itself - demonstrated here, not just documented in a comment, so a future
reader of this suite sees the caller-dependent behavior directly.
"""


def test_unexpected_error_during_audit_write_propagates_and_leaves_session_uncommitted():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    row = _allowed_row(["SYSTEM", "HR_ADMIN", "RECRUITER"], requires_reason=False)
    service, allowed_transition_repo, campaign_candidate_repo, audit_service = _make_transition_env(candidate, row)
    audit_service.log.side_effect = RuntimeError("transient DB error writing audit_log")

    with pytest.raises(RuntimeError, match="transient DB error writing audit_log"):
        service.transition(candidate.id, PipelineStage.REJECTED, Actor.system())

    # The stage-history insert and the pipeline_stage update DID happen
    # in-session before the failure - this is "uncommitted", not
    # "half-applied and abandoned".
    campaign_candidate_repo.create_stage_history_idempotent.assert_called_once()
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    # Neither commit nor rollback is called by transition() itself on this
    # path - whatever called transition() is entirely responsible for
    # deciding what happens to the still-open transaction.
    campaign_candidate_repo.commit.assert_not_called()
    campaign_candidate_repo.rollback.assert_not_called()
