"""
CampaignService.override_candidate_stage - previously zero test coverage
anywhere in this codebase. This is the third of three independent
pipeline-stage-writing paths found capable of reaching to_stage=INTERVIEW
(besides StageTransitionService.transition() and
PipelineTransitionService.transition_stage()) - discovered live, via a
"Stalled Candidates" override moving a candidate to INTERVIEW with no
interview_schedules row created, mid frontend integration testing.

These tests focus specifically on the INTERVIEW-entry hook this class
needed (mirroring the equivalent tests on the other two engines), not a
full behavioral test suite for override_candidate_stage.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.pipeline import PipelineStage
from app.schemas.campaign.campaign_monitoring_schema import StageOverrideRequest
from app.services.campaign.campaign_service import CampaignService


def _make_candidate(pipeline_stage):
    return SimpleNamespace(
        id=uuid4(), campaign_id=uuid4(), pipeline_stage=pipeline_stage,
        composite_score=None, deterministic_score=None, semantic_score=None,
        ai_evaluation=None, decision_type=None, decision_source=None,
        decision_reason=None, decision_details=None,
    )


def _make_service(campaign, candidate):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_campaign_candidate.return_value = candidate

    interview_schedule_repo = MagicMock()

    service = CampaignService(
        campaign_repo=campaign_repo,
        jd_repo=MagicMock(),
        audit_service=MagicMock(),
        config_repo=MagicMock(),
        preset_repo=MagicMock(),
        db=MagicMock(),
        interview_schedule_repo=interview_schedule_repo,
    )
    return service, campaign_repo, interview_schedule_repo


def test_override_to_interview_via_natural_next_stage_creates_pending_interview_schedule():
    candidate = _make_candidate(PipelineStage.HM_REVIEW)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="might match for the role"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.get_or_create_pending.assert_called_once_with(candidate.id)
    campaign_repo.transition_candidate_stage.assert_called_once()
    assert campaign_repo.transition_candidate_stage.call_args.args[1] == PipelineStage.INTERVIEW


def test_override_to_interview_via_explicit_target_stage_creates_pending_interview_schedule():
    candidate = _make_candidate(PipelineStage.SHORTLISTED)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="might match for the role", target_stage="INTERVIEW"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.get_or_create_pending.assert_called_once_with(candidate.id)


def test_override_to_a_non_interview_stage_never_touches_interview_schedule_repo():
    candidate = _make_candidate(PipelineStage.SCREENING)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="natural progression"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.get_or_create_pending.assert_not_called()


def test_interview_schedule_repo_defaults_from_db_when_not_explicitly_supplied():
    """
    Same defaulted-from-db convention as circuit_breaker_repo/
    dead_letter_queue_repo above it - existing get_campaign_service DI
    wiring (which doesn't pass interview_schedule_repo explicitly) must
    still end up with a real, working repo, not None.
    """
    service = CampaignService(
        campaign_repo=MagicMock(), jd_repo=MagicMock(), audit_service=MagicMock(),
        config_repo=MagicMock(), preset_repo=MagicMock(), db=MagicMock(),
    )

    assert service.interview_schedule_repo is not None


# ----------------------------------------------------------------------
# M12 cascading-cancellation follow-up: override_candidate_stage's
# INTERVIEW-exit cascade. This is the third of the 3 places a candidate
# can leave INTERVIEW from - REJECTED is unreachable via this path at all
# (in _OVERRIDE_FORBIDDEN_TARGETS, has its own dedicated flow), so only
# SELECTED (the natural next stage) and SHORTLISTED (explicit target_stage
# only) are exercisable here.
# ----------------------------------------------------------------------

def test_override_from_interview_to_selected_natural_next_cancels_active_rounds():
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(
        id=candidate.campaign_id, hiring_manager_id="hm-1", max_candidates=None, status=None,
    )
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)
    # target==SELECTED also runs _close_if_all_positions_filled - give it a
    # real (uncapped) campaign row so that unrelated check short-circuits
    # cleanly instead of comparing two MagicMocks.
    campaign_repo.get_by_id_for_update.return_value = campaign

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="ready to select"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.cancel_active_rounds.assert_called_once_with(
        candidate.id,
        reason="Candidate outcome finalized: SELECTED",
        changed_by="hr-1",
        changed_by_role="HR_ADMIN",
    )


def test_override_from_interview_to_shortlisted_explicit_target_cancels_active_rounds():
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="sending back to shortlist", target_stage="SHORTLISTED"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.cancel_active_rounds.assert_called_once_with(
        candidate.id,
        reason="Candidate outcome finalized: SHORTLISTED",
        changed_by="hr-1",
        changed_by_role="HR_ADMIN",
    )


def test_override_from_interview_to_hold_never_cascades():
    """HOLD is a reversible pause (has a resume-edge back to INTERVIEW) - must not cascade-cancel."""
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    service.override_candidate_stage(
        campaign.id, candidate.id,
        StageOverrideRequest(reason="pausing candidate", target_stage="HOLD"),
        actor_id="hr-1", actor_role="HR_ADMIN",
    )

    interview_schedule_repo.cancel_active_rounds.assert_not_called()


# ----------------------------------------------------------------------
# Epic 5 Step 2 - CANDIDATE_SELECTED email hook. override_candidate_stage
# is the 3rd of 3 real pipeline-stage-writing paths, and this one commits
# internally, so the hook lives in this same method (unlike
# PipelineTransitionService.transition_stage(), which never commits).
# ----------------------------------------------------------------------

def test_override_from_interview_to_selected_queues_a_candidate_selected_email_after_commit():
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(
        id=candidate.campaign_id, hiring_manager_id="hm-1", max_candidates=None, status=None,
    )
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)
    campaign_repo.get_by_id_for_update.return_value = campaign

    with patch("app.services.campaign.campaign_service.queue_candidate_selected_email") as mock_queue:
        service.override_candidate_stage(
            campaign.id, candidate.id,
            StageOverrideRequest(reason="ready to select"),
            actor_id="hr-1", actor_role="HR_ADMIN",
        )

    mock_queue.assert_called_once_with(campaign_repo.db, candidate)
    campaign_repo.commit.assert_called_once()


def test_override_from_interview_to_shortlisted_never_queues_a_selected_email():
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    with patch("app.services.campaign.campaign_service.queue_candidate_selected_email") as mock_queue:
        service.override_candidate_stage(
            campaign.id, candidate.id,
            StageOverrideRequest(reason="sending back to shortlist", target_stage="SHORTLISTED"),
            actor_id="hr-1", actor_role="HR_ADMIN",
        )

    mock_queue.assert_not_called()


# ----------------------------------------------------------------------
# Epic 5 follow-up - manual re-score trigger. override_candidate_stage
# commits internally (unlike PipelineTransitionService.transition_stage()),
# so both halves - the cascade-cancel and the Celery enqueue - live in
# this same method, mirroring the CANDIDATE_SELECTED email hook above it.
# ----------------------------------------------------------------------

def test_override_from_hold_to_screening_cancels_active_rounds_and_enqueues_rescore():
    candidate = _make_candidate(PipelineStage.HOLD)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    with patch("app.services.campaign.campaign_service.enqueue_manual_rescore") as mock_rescore:
        service.override_candidate_stage(
            campaign.id, candidate.id,
            StageOverrideRequest(reason="returned for re-evaluation", target_stage="SCREENING"),
            actor_id="hr-1", actor_role="HR_ADMIN",
        )

    interview_schedule_repo.cancel_active_rounds.assert_called_once_with(
        candidate.id,
        reason="Candidate returned to SCREENING for re-evaluation",
        changed_by="hr-1",
        changed_by_role="HR_ADMIN",
    )
    mock_rescore.assert_called_once_with(campaign_repo.db, candidate)
    campaign_repo.commit.assert_called_once()


def test_override_from_uploaded_natural_next_stage_never_cascades_or_enqueues():
    """UPLOADED's natural next stage is SCREENING (the automated path) - must never get this hook."""
    candidate = _make_candidate(PipelineStage.UPLOADED)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    with patch("app.services.campaign.campaign_service.enqueue_manual_rescore") as mock_rescore:
        service.override_candidate_stage(
            campaign.id, candidate.id,
            StageOverrideRequest(reason="ready for screening"),
            actor_id="hr-1", actor_role="HR_ADMIN",
        )

    interview_schedule_repo.cancel_active_rounds.assert_not_called()
    mock_rescore.assert_not_called()


def test_override_to_a_non_screening_target_never_enqueues_rescore():
    candidate = _make_candidate(PipelineStage.INTERVIEW)
    campaign = SimpleNamespace(id=candidate.campaign_id, hiring_manager_id="hm-1")
    service, campaign_repo, interview_schedule_repo = _make_service(campaign, candidate)

    with patch("app.services.campaign.campaign_service.enqueue_manual_rescore") as mock_rescore:
        service.override_candidate_stage(
            campaign.id, candidate.id,
            StageOverrideRequest(reason="sending back to shortlist", target_stage="SHORTLISTED"),
            actor_id="hr-1", actor_role="HR_ADMIN",
        )

    mock_rescore.assert_not_called()
