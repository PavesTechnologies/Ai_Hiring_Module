"""
Recruiter/HR actions on candidates.

Deliberately a separate router from campaign_candidate.py: that module belongs
to M07/M10 and has been overwritten by pulls more than once, so M11's additions
live here to keep merges clean.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.candidate_actions import (
    get_bulk_stage_move_service,
    get_candidate_note_service,
    get_override_revert_service,
)
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.campaign.bulk_stage_move_schema import (
    BulkStageMoveRequest,
    BulkStageMoveResultResponse,
    ManualRejectRequest,
    SingleStageMoveRequest,
    SingleStageMoveResultResponse,
)
from app.schemas.campaign.candidate_note_schema import (
    CandidateNoteCountsRequest,
    CandidateNoteCountsResponse,
    CandidateNoteCreateRequest,
    CandidateNoteResponse,
    CandidateNoteUpdateRequest,
)
from app.schemas.campaign.override_revert_schema import (
    OverrideRevertRequest,
    OverrideRevertResultResponse,
)
from app.schemas.response import APIResponse
from app.services.campaign.bulk_stage_move_service import BulkStageMoveService
from app.services.campaign.candidate_note_service import CandidateNoteService
from app.services.campaign.override_revert_service import OverrideRevertService

router = APIRouter(prefix="/candidate-actions", tags=["Candidate Actions"])


@router.post(
    "/campaigns/{campaign_id}/bulk-stage-move",
    response_model=APIResponse[BulkStageMoveResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Move multiple candidates to the next stage",
    description=(
        "All selected candidates must currently share the same stage. "
        "Applies one shared reason, runs every move through the validated transition "
        "engine, and records the batch as a single audit entry."
    ),
)
def bulk_stage_move(
    campaign_id: UUID,
    request: BulkStageMoveRequest,
    service: BulkStageMoveService = Depends(get_bulk_stage_move_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.bulk_move(
        campaign_id=campaign_id,
        campaign_candidate_ids=request.campaign_candidate_ids,
        target_stage=request.target_stage,
        reason=request.reason,
        actor_id=user.user_id,
        actor_role=UserRole.HR_ADMIN.value,
    )
    return APIResponse.ok(data=result, message=result.detail)


@router.post(
    "/campaigns/{campaign_id}/candidates/{campaign_candidate_id}/stage-move",
    response_model=APIResponse[SingleStageMoveResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Move one candidate to another stage",
    description=(
        "Moves a single candidate with a mandatory reason. The target "
        "must be a legal transition from the candidate's current stage; the openings cap, "
        "stage history and decision record all apply exactly as they do to a bulk move."
    ),
)
def single_stage_move(
    campaign_id: UUID,
    campaign_candidate_id: UUID,
    request: SingleStageMoveRequest,
    service: BulkStageMoveService = Depends(get_bulk_stage_move_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    result = service.move_one(
        campaign_id=campaign_id,
        campaign_candidate_id=campaign_candidate_id,
        target_stage=request.target_stage,
        reason=request.reason,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message=result.detail)


@router.post(
    "/campaigns/{campaign_id}/candidates/{campaign_candidate_id}/reject",
    response_model=APIResponse[SingleStageMoveResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Manually reject a candidate with a reason",
    description=(
        "Rejects a candidate directly from the candidate list. Routed "
        "through the same transition engine as any other stage move, so the rejection is "
        "recorded as a decision rather than a bare stage change."
    ),
)
def manual_reject(
    campaign_id: UUID,
    campaign_candidate_id: UUID,
    request: ManualRejectRequest,
    service: BulkStageMoveService = Depends(get_bulk_stage_move_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    result = service.reject_one(
        campaign_id=campaign_id,
        campaign_candidate_id=campaign_candidate_id,
        reason=request.reason,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message=result.detail)


@router.get(
    "/candidates/{campaign_candidate_id}/notes",
    response_model=APIResponse[list[CandidateNoteResponse]],
    status_code=status.HTTP_200_OK,
    summary="List recruiter notes on a candidate",
    description="Newest first. Deleted notes are never returned.",
)
def list_candidate_notes(
    campaign_candidate_id: UUID,
    service: CandidateNoteService = Depends(get_candidate_note_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.list_notes(campaign_candidate_id))


@router.post(
    "/candidates/{campaign_candidate_id}/notes",
    response_model=APIResponse[CandidateNoteResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Add a recruiter note to a candidate",
    description="Notes are scoped to this candidate within this campaign.",
)
def add_candidate_note(
    campaign_candidate_id: UUID,
    request: CandidateNoteCreateRequest,
    service: CandidateNoteService = Depends(get_candidate_note_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    result = service.add_note(
        campaign_candidate_id, request.note_text,
        actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Note added.")


@router.patch(
    "/notes/{note_id}",
    response_model=APIResponse[CandidateNoteResponse],
    status_code=status.HTTP_200_OK,
    summary="Edit a recruiter note",
    description="Authors edit their own notes; HR_ADMIN may edit any.",
)
def edit_candidate_note(
    note_id: UUID,
    request: CandidateNoteUpdateRequest,
    service: CandidateNoteService = Depends(get_candidate_note_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    result = service.edit_note(
        note_id, request.note_text,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        is_hr_admin=UserRole.HR_ADMIN.value in (user.roles or []),
    )
    return APIResponse.ok(data=result, message="Note updated.")


@router.delete(
    "/notes/{note_id}",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Delete a recruiter note",
    description=(
        "Soft delete. The note leaves the list but is retained for audit."
    ),
)
def delete_candidate_note(
    note_id: UUID,
    service: CandidateNoteService = Depends(get_candidate_note_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    service.delete_note(
        note_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        is_hr_admin=UserRole.HR_ADMIN.value in (user.roles or []),
    )
    return APIResponse.ok(data={}, message="Note deleted.")


@router.post(
    "/note-counts",
    response_model=APIResponse[CandidateNoteCountsResponse],
    status_code=status.HTTP_200_OK,
    summary="Note counts for a page of candidates",
    description=(
        "One query for the whole page, so the list's note badges do not "
        "cost one round trip per row. POST because the id list can exceed a sane URL length."
    ),
)
def candidate_note_counts(
    request: CandidateNoteCountsRequest,
    service: CandidateNoteService = Depends(get_candidate_note_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    counts = service.note_counts(request.campaign_candidate_ids)
    return APIResponse.ok(data=CandidateNoteCountsResponse(counts=counts))


@router.post(
    "/candidates/{campaign_candidate_id}/clear-override",
    response_model=APIResponse[OverrideRevertResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Clear an HR override and restore the automated decision",
    description=(
        "Reverses an HR override, restoring the deterministic or semantic "
        "decision it replaced (read back from decision_details, never recomputed). Only "
        "available while the candidate is still in SCREENING, i.e. before the override has "
        "produced a new outcome. HR_ADMIN only."
    ),
)
def clear_override(
    campaign_candidate_id: UUID,
    request: OverrideRevertRequest,
    service: OverrideRevertService = Depends(get_override_revert_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.revert_override(
        campaign_candidate_id=campaign_candidate_id,
        reason=request.reason,
        actor_id=user.user_id,
        actor_role=UserRole.HR_ADMIN.value,
    )
    return APIResponse.ok(data=result, message=result.detail)
