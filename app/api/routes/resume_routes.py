from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Security, UploadFile, status
from pydantic import ValidationError

from app.dependencies.resume import (
    get_resume_intake_service,
    get_resume_processing_status_service,
)
from app.enums.constants import Jurisdiction, UserRole
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.resume.request import ResumeUploadRequest
from app.schemas.resume.response import (
    ResumeProcessingStatusResponse,
    ResumeUploadAcceptedResponse,
)
from app.schemas.response import APIResponse
from app.services.resume.resume_intake_service import ResumeIntakeService
from app.services.resume.resume_processing_status_service import ResumeProcessingStatusService

router = APIRouter(
    prefix="/resumes",
    tags=["Resume Intake"],
)


@router.post(
    "",
    response_model=APIResponse[ResumeUploadAcceptedResponse],
    status_code=status.HTTP_201_CREATED,
)
def upload_resume(
    request: Request,
    campaign_id: UUID = Form(...),
    candidate_full_name: str = Form(..., min_length=1, max_length=255),
    candidate_email: str = Form(..., max_length=255),
    candidate_phone: str | None = Form(default=None, max_length=50),
    jurisdiction: str = Form(default=Jurisdiction.GLOBAL.value),
    consent_confirmed: bool = Form(...),
    file: UploadFile = File(...),
    service: ResumeIntakeService = Depends(get_resume_intake_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Validates, stores the file, creates/reuses the candidate, inserts the
    campaign_candidates pipeline record, and enqueues the RESUME_PARSE
    background task — the response's parse_status still reads PENDING
    since parsing runs asynchronously after this call returns; poll
    task_id to observe progress (polling endpoint itself is Phase 9).
    """
    try:
        validated = ResumeUploadRequest(
            campaign_id=campaign_id,
            candidate_full_name=candidate_full_name,
            candidate_email=candidate_email,
            candidate_phone=candidate_phone,
            jurisdiction=jurisdiction,
            consent_confirmed=consent_confirmed,
        )
    except ValidationError as exc:
        raise BadRequestError(str(exc)) from exc

    file_bytes = file.file.read()
    filename = file.filename or "resume"

    resume, campaign_candidate, campaign, task_id = service.upload_resume(
        campaign_id=validated.campaign_id,
        file_bytes=file_bytes,
        filename=filename,
        candidate_full_name=validated.candidate_full_name,
        candidate_email=validated.candidate_email,
        jurisdiction=validated.jurisdiction,
        uploaded_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        content_type=file.content_type,
        candidate_phone=validated.candidate_phone,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    masked_name = validated.candidate_full_name.split(" ")[0]

    return APIResponse.ok(
        data=ResumeUploadAcceptedResponse(
            resume_id=resume.id,
            campaign_candidate_id=campaign_candidate.id,
            task_id=task_id,
            candidate_name_masked=masked_name,
            file_name=filename,
            campaign_name=campaign.name,
            pipeline_stage=campaign_candidate.pipeline_stage.value,
            parse_status=resume.parse_status.value,
        ),
        message="Resume uploaded successfully and queued for processing.",
    )


@router.get(
    "/processing-status/{task_id}",
    response_model=APIResponse[ResumeProcessingStatusResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_processing_status(
    task_id: UUID,
    service: ResumeProcessingStatusService = Depends(get_resume_processing_status_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return APIResponse.ok(
        data=service.get_status(task_id),
        message="Processing status retrieved successfully.",
    )
