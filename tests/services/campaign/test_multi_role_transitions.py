"""
A user holding several roles the route allows (RECRUITER + HR_ADMIN here): a
stage transition is made if it permits ANY of them, and the role that
permitted it is what the stage history and audit record. Covers single move,
manual reject, the allowed-transitions list, re-parse of a rejected
candidate, and resume resubmission - plus who may add a candidate to a
campaign (HR_ADMIN, or the campaign's assigned recruiter).

The real PipelineTransitionService runs over mocked repositories; no
database, storage, Celery or email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.pipeline_transition_exceptions import (
    ForbiddenPipelineRoleException, InvalidPipelineTransitionException,
)
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage
from app.services.campaign.bulk_stage_move_service import BulkStageMoveService
from app.services.campaign.pipeline_transition_service import PipelineTransitionService, permitted_role

BOTH = ["HR_ADMIN", "RECRUITER"]                 # broadest first, as authorising_roles() orders them


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


def _transitions(*rows):
    """rows: (from_stage, to_stage, allowed_roles) - previous_stage ignored."""
    table = {(f, t): SimpleNamespace(allowed_roles=list(roles), requires_reason=False, to_stage=t, notes=None)
             for f, t, roles in rows}
    repo = MagicMock()
    repo.get.side_effect = lambda f, t, previous_stage=None: table.get((f, t))
    repo.list_eligible.side_effect = lambda f, previous_stage=None: [r for (ff, _), r in table.items() if ff == f]
    return repo


def _cc(stage, campaign_id=None):
    return SimpleNamespace(id=uuid4(), campaign_id=campaign_id or uuid4(), candidate_id=uuid4(), resume_id=uuid4(),
                           pipeline_stage=stage, previous_stage=None)


def _engine(repo):
    return PipelineTransitionService(allowed_transition_repo=repo, campaign_candidate_repo=MagicMock(),
                                     audit_service=MagicMock(), interview_schedule_repo=MagicMock())


def _mover(repo, cc):
    engine = _engine(repo)
    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = cc
    return BulkStageMoveService(campaign_candidate_repo=cc_repo, pipeline_transition_service=engine,
                                audit_service=MagicMock(), notifier=MagicMock()), engine


# --- the rule itself ---------------------------------------------------------------------

def test_permitted_role_is_the_first_held_role_the_transition_allows():
    cc = _cc(PipelineStage.SCREENING)
    only_recruiter = _transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, ["RECRUITER"]))
    both = _transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, ["RECRUITER", "HR_ADMIN"]))
    assert permitted_role(only_recruiter, cc, PipelineStage.REJECTED, BOTH) == "RECRUITER"
    assert permitted_role(both, cc, PipelineStage.REJECTED, BOTH) == "HR_ADMIN"


def test_permitted_role_raises_like_the_transition_engine():
    cc = _cc(PipelineStage.SCREENING)
    hm_only = _transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, ["HIRING_MANAGER"]))
    with pytest.raises(ForbiddenPipelineRoleException) as exc:
        permitted_role(hm_only, cc, PipelineStage.REJECTED, BOTH)
    assert exc.value.status_code == 403 and "HR_ADMIN" in str(exc.value) and "RECRUITER" in str(exc.value)
    with pytest.raises(InvalidPipelineTransitionException):
        permitted_role(_transitions(), cc, PipelineStage.REJECTED, BOTH)


# --- manual reject (route allows HR_ADMIN and RECRUITER) -----------------------------------

@pytest.mark.parametrize("allowed, recorded", [(["RECRUITER"], "RECRUITER"), (["HR_ADMIN"], "HR_ADMIN")])
def test_reject_with_both_roles_moves_on_whichever_the_transition_permits(allowed, recorded):
    cc = _cc(PipelineStage.SCREENING)
    mover, engine = _mover(_transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, allowed)), cc)

    result = mover.reject_one(campaign_id=cc.campaign_id, campaign_candidate_id=cc.id, reason="Not a fit.",
                              actor_id="u-1", actor_role="HR_ADMIN", actor_roles=BOTH)

    assert result.to_stage == "REJECTED"
    engine.campaign_candidate_repo.update_pipeline_stage.assert_called_once_with(cc, PipelineStage.REJECTED)
    assert engine.audit_service.log.call_args.kwargs["actor_role"] == recorded       # the transition's audit
    assert mover.audit_service.log.call_args.kwargs["actor_role"] == recorded        # the move's own audit


def test_reject_is_refused_when_no_held_role_permits_it():
    cc = _cc(PipelineStage.SCREENING)
    mover, engine = _mover(_transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, ["HIRING_MANAGER"])), cc)
    with pytest.raises(ForbiddenPipelineRoleException):
        mover.reject_one(campaign_id=cc.campaign_id, campaign_candidate_id=cc.id, reason="Not a fit.",
                         actor_id="u-1", actor_role="HR_ADMIN", actor_roles=BOTH)
    engine.campaign_candidate_repo.update_pipeline_stage.assert_not_called()
    mover.campaign_candidate_repo.rollback.assert_called_once()


# --- single move (route allows HIRING_MANAGER and RECRUITER: HR_ADMIN never counts) ---------

def test_single_move_with_both_roles_uses_recruiter_the_one_the_route_allows():
    cc = _cc(PipelineStage.SCREENING)
    mover, engine = _mover(_transitions((PipelineStage.SCREENING, PipelineStage.SHORTLISTED, ["RECRUITER"])), cc)

    # authorising_roles() for RECRUITER + HR_ADMIN on this route is just ["RECRUITER"]
    mover.move_one(campaign_id=cc.campaign_id, campaign_candidate_id=cc.id, target_stage="SHORTLISTED",
                   reason=None, actor_id="u-1", actor_role="RECRUITER", actor_roles=["RECRUITER"])

    assert engine.audit_service.log.call_args.kwargs["actor_role"] == "RECRUITER"
    assert mover.audit_service.log.call_args.kwargs["actor_role"] == "RECRUITER"


def test_single_move_without_actor_roles_keeps_the_single_role_check():
    cc = _cc(PipelineStage.SCREENING)
    mover, _ = _mover(_transitions((PipelineStage.SCREENING, PipelineStage.SHORTLISTED, ["RECRUITER"])), cc)
    with pytest.raises(ForbiddenPipelineRoleException):
        mover.move_one(campaign_id=cc.campaign_id, campaign_candidate_id=cc.id, target_stage="SHORTLISTED",
                       reason=None, actor_id="u-1", actor_role="HR_ADMIN")


# --- allowed transitions (route allows all three) -----------------------------------------

def test_allowed_transitions_list_what_any_held_role_permits():
    cc = _cc(PipelineStage.SCREENING)
    repo = _transitions((PipelineStage.SCREENING, PipelineStage.REJECTED, ["RECRUITER"]),
                        (PipelineStage.SCREENING, PipelineStage.HOLD, ["HR_ADMIN"]),
                        (PipelineStage.SCREENING, PipelineStage.HM_REVIEW, ["HIRING_MANAGER"]))
    mover, _ = _mover(repo, cc)

    result = mover.get_allowed_transitions(campaign_id=cc.campaign_id, campaign_candidate_id=cc.id,
                                           actor_role="HR_ADMIN", actor_roles=BOTH)

    assert [t.to_stage for t in result.allowed_transitions] == ["HOLD", "REJECTED"]


# --- re-parse a rejected candidate (route allows HR_ADMIN and RECRUITER) --------------------

def _reparse_service(repo, cc):
    from app.services.resume.resume_reparse_service import ResumeReparseService
    service = ResumeReparseService.__new__(ResumeReparseService)
    campaign = SimpleNamespace(id=cc.campaign_id, jd_id=uuid4(), prompt_template_id=uuid4())
    resume = SimpleNamespace(id=cc.resume_id, parse_status=ParseStatus.PARSED, parsed_json={})
    service.db = MagicMock()
    service.db.execute.return_value.scalar_one.return_value = 1
    service.db.execute.return_value.all.return_value = []
    service.campaign_candidate_repo = MagicMock(get_by_id=MagicMock(return_value=cc),
                                                get_by_resume_id=MagicMock(return_value=[cc]))
    service.campaign_repo = MagicMock(get_by_id=MagicMock(return_value=campaign))
    service.resume_repo = MagicMock(get_by_id=MagicMock(return_value=resume))
    service.audit_service = MagicMock()
    service.allowed_transition_repo = repo
    return service


def _reparse(service, cc, reason="Competency section was missed"):
    with patch("app.tasks.resume_processing_tasks.process_resume_document") as task:
        result = service.request(campaign_candidate_id=cc.id, actor_id="u-1", actor_role="HR_ADMIN",
                                 reason=reason, actor_roles=BOTH)
    return result, task.apply_async.call_args.kwargs["kwargs"]["reparse"]


def test_reparse_with_both_roles_moves_a_rejected_candidate_and_records_recruiter():
    cc = _cc(PipelineStage.REJECTED)
    service = _reparse_service(_transitions((PipelineStage.REJECTED, PipelineStage.SCREENING, ["RECRUITER"])), cc)

    result, reparse = _reparse(service, cc)

    assert result["moves_to_screening"] is True and result["stays_rejected"] is False
    assert reparse["move_to_screening"] is True and reparse["actor_role"] == "RECRUITER"   # what finish() moves as
    assert service.audit_service.log.call_args.kwargs["actor_role"] == "RECRUITER"


def test_reparse_with_both_roles_keeps_rejected_when_neither_may_move_it():
    cc = _cc(PipelineStage.REJECTED)                       # e.g. after fraud review: HIRING_MANAGER only
    service = _reparse_service(_transitions((PipelineStage.REJECTED, PipelineStage.SCREENING,
                                             ["HIRING_MANAGER"])), cc)

    result, reparse = _reparse(service, cc, reason=None)

    assert result["moves_to_screening"] is False and result["stays_rejected"] is True
    assert reparse["actor_role"] == "HR_ADMIN"                                            # the route's role
    assert service.audit_service.log.call_args.kwargs["actor_role"] == "HR_ADMIN"


def test_reparse_with_both_roles_still_needs_a_reason_to_move_a_rejected_candidate():
    cc = _cc(PipelineStage.REJECTED)
    service = _reparse_service(_transitions((PipelineStage.REJECTED, PipelineStage.SCREENING, ["RECRUITER"])), cc)
    with pytest.raises(CampaignException) as exc:
        _reparse(service, cc, reason="  ")
    assert exc.value.status_code == 422


# --- resubmission (route allows HR_ADMIN and RECRUITER) ------------------------------------

class _StopAfterTransition(Exception):
    pass


def _resubmission_service(repo, cc):
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    service = CampaignCandidateService.__new__(CampaignCandidateService)
    service.campaign_candidate_repo = MagicMock()
    service.campaign_candidate_repo.get_by_id.return_value = cc
    service.pipeline_transition_service = _engine(repo)
    service.file_validation_service = MagicMock()
    service.file_validation_service.validate.side_effect = _StopAfterTransition()   # nothing stored past the move
    service.storage_service = MagicMock()
    return service


@pytest.mark.parametrize("allowed, recorded", [(["RECRUITER"], "RECRUITER"), (["HR_ADMIN"], "HR_ADMIN")])
def test_resubmission_with_both_roles_moves_on_whichever_the_transition_permits(allowed, recorded):
    cc = _cc(PipelineStage.SCREENING)
    service = _resubmission_service(_transitions((PipelineStage.SCREENING, PipelineStage.UPLOADED, allowed)), cc)

    with pytest.raises(Exception):
        service.update_resume_for_resubmission(cc.id, file_bytes=b"%PDF", filename="cv.pdf", actor_id="u-1",
                                               actor_role="HR_ADMIN", actor_roles=BOTH)

    engine = service.pipeline_transition_service
    engine.campaign_candidate_repo.update_pipeline_stage.assert_called_once_with(cc, PipelineStage.UPLOADED)
    assert engine.audit_service.log.call_args.kwargs["actor_role"] == recorded
    service.storage_service.upload_file.assert_not_called()


def test_resubmission_is_a_403_when_no_held_role_permits_it():
    cc = _cc(PipelineStage.SCREENING)
    service = _resubmission_service(_transitions((PipelineStage.SCREENING, PipelineStage.UPLOADED,
                                                  ["HIRING_MANAGER"])), cc)
    with pytest.raises(CampaignException) as exc:
        service.update_resume_for_resubmission(cc.id, file_bytes=b"%PDF", filename="cv.pdf", actor_id="u-1",
                                               actor_role="HR_ADMIN", actor_roles=BOTH)
    assert exc.value.status_code == 403
    service.campaign_candidate_repo.rollback.assert_called_once()
    service.file_validation_service.validate.assert_not_called()


# --- adding a candidate to a campaign: HR_ADMIN or the assigned recruiter --------------------

def _add_service(recruiter_id="rec-1"):
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    service = CampaignCandidateService.__new__(CampaignCandidateService)
    service.campaign_candidate_repo = MagicMock()
    service.campaign_repo = MagicMock()
    service.campaign_repo.get_by_id.return_value = SimpleNamespace(id=uuid4(), recruiter_id=recruiter_id)
    service.create_campaign_candidate = MagicMock(return_value="created")
    return service


def _add(service, actor_id, roles):
    from app.schemas.campaign.campaign_candidate_schema import CampaignCandidateCreateRequest
    request = CampaignCandidateCreateRequest(campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4())
    return service.add_campaign_candidate(request, actor_id=actor_id, actor_role=roles[0], actor_roles=roles)


@pytest.mark.parametrize("actor_id, roles", [
    ("rec-1", ["RECRUITER"]),                 # the assigned recruiter
    ("hr-1", ["HR_ADMIN"]),                   # any HR admin
    ("hr-1", BOTH),                           # RECRUITER + HR_ADMIN, not assigned: HR_ADMIN allows it
    ("rec-1", BOTH),
])
def test_hr_admin_or_the_assigned_recruiter_can_add_a_candidate(actor_id, roles):
    service = _add_service()
    assert _add(service, actor_id, roles) == "created"
    service.campaign_candidate_repo.commit.assert_called_once()


def test_another_recruiter_cannot_add_a_candidate():
    service = _add_service()
    with pytest.raises(CampaignException) as exc:
        _add(service, "rec-2", ["RECRUITER"])
    assert exc.value.status_code == 403
    service.create_campaign_candidate.assert_not_called()
    service.campaign_candidate_repo.commit.assert_not_called()


def test_a_missing_campaign_still_gets_the_usual_not_found():
    service = _add_service()
    service.campaign_repo.get_by_id.return_value = None
    service.create_campaign_candidate.side_effect = CampaignException("Campaign not found.", 404)
    with pytest.raises(CampaignException) as exc:
        _add(service, "rec-2", ["RECRUITER"])
    assert exc.value.status_code == 404
