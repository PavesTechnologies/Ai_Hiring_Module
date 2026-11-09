from app.services.campaign_access import CampaignAccess
from app.db.session import get_db
from sqlalchemy.orm import Session
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.resume import get_upload_decision_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.models.upload_decisions import UploadDecisionStatus
from app.schemas.resume.upload_decision import (
    UploadDecisionDecideRequest,
    UploadDecisionDecideResponse,
    UploadDecisionListResponse,
)
from app.schemas.response import APIResponse
from app.services.resume.upload_decision_service import UploadDecisionService

router = APIRouter(tags=["Upload Decisions"])

_StatusFilter = Literal["PENDING", "KEPT_EXISTING", "NEW_VERSION_USED", "EXPIRED_KEPT_EXISTING", "ALL"]


@router.get(
    "/campaigns/{campaign_id}/upload-decisions",
    response_model=APIResponse[UploadDecisionListResponse],
    status_code=status.HTTP_200_OK,
    summary="A campaign's held bulk-upload files waiting for 'keep existing / use new version'",
)
def list_campaign_upload_decisions(
    campaign_id: UUID,
    decision_status: _StatusFilter = Query(default=UploadDecisionStatus.PENDING.value, alias="status"),
    service: UploadDecisionService = Depends(get_upload_decision_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
    access_db: Session = Depends(get_db),
):
    """Newest first. status=ALL returns every status (decided and expired too)."""
    CampaignAccess(access_db).require_campaign(user, campaign_id)
    items = service.list_for_campaign(campaign_id, None if decision_status == "ALL" else decision_status)
    return APIResponse.ok(
        data=UploadDecisionListResponse(items=items, total=len(items)),
        message="Upload decisions retrieved successfully.",
    )


@router.get(
    "/bulk-uploads/{bulk_upload_job_id}/decisions",
    response_model=APIResponse[UploadDecisionListResponse],
    status_code=status.HTTP_200_OK,
    summary="Every upload decision (any status) raised by one bulk upload",
)
def list_bulk_upload_decisions(
    bulk_upload_job_id: UUID,
    service: UploadDecisionService = Depends(get_upload_decision_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
    access_db: Session = Depends(get_db),
):
    CampaignAccess(access_db).require_bulk_job(user, bulk_upload_job_id)
    items = service.list_for_job(bulk_upload_job_id)
    return APIResponse.ok(
        data=UploadDecisionListResponse(items=items, total=len(items)),
        message="Upload decisions retrieved successfully.",
    )


@router.post(
    "/upload-decisions/{decision_id}/decide",
    response_model=APIResponse[UploadDecisionDecideResponse],
    status_code=status.HTTP_200_OK,
    summary="Keep the existing resume, or use the held file as the candidate's new version",
)
def decide_upload_decision(
    decision_id: UUID,
    body: UploadDecisionDecideRequest,
    service: UploadDecisionService = Depends(get_upload_decision_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
    access_db: Session = Depends(get_db),
):
    """
    Only the uploader of the bulk file or an HR admin may decide (403
    otherwise); an already-decided or expired decision is 409.
    keep_existing: the held file is deleted. use_new_version: it becomes the
    candidate's next resume version for this campaign, parsed and re-scored
    with the stage unchanged; other campaigns are marked score out of date.
    """
    CampaignAccess(access_db).require_upload_decision(user, decision_id)
    result = service.decide(decision_id, body.choice, actor_id=user.user_id, actor_roles=list(user.roles or []))
    message = (
        "Existing resume kept - the new file was discarded."
        if body.choice == "keep_existing"
        else "New resume version created and queued for processing - the stage is unchanged."
    )
    return APIResponse.ok(data=result, message=message)
