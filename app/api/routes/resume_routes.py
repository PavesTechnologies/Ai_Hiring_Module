from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Security, UploadFile, status
from pydantic import ValidationError

from app.dependencies.resume import (
    get_resume_cleanup_service,
    get_resume_intake_service,
    get_resume_monitoring_service,
    get_resume_processing_status_service,
    get_resume_service,
)
from app.enums.constants import Jurisdiction, UserRole
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.models.candidates import ParseStatus
from app.schemas.resume.monitoring import (
    ParseAttemptItem,
    ResumeDetailResponse,
    ResumeListResponse,
    ResumeListWithPipelineResponse,
    ResumeParsedJsonResponse,
    ResumeTimelineResponse,
)
from app.schemas.resume.request import ResumeUploadRequest
from app.schemas.resume.response import (
    ResumeDownloadUrlResponse,
    ResumeProcessingStatusResponse,
    ResumeRetryResponse,
    ResumeUploadAcceptedResponse,
    ResumeVersionComparisonResponse,
    ResumeVersionHistoryResponse,
)
from app.schemas.response import APIResponse
from app.services.resume.resume_cleanup_service import ResumeCleanupService
from app.services.resume.resume_intake_service import ResumeIntakeService
from app.services.resume.resume_monitoring_service import ResumeMonitoringService
from app.services.resume.resume_processing_status_service import ResumeProcessingStatusService
from app.services.resume.resume_upload_service import ResumeUploadService

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
    resolution: Literal["use_existing", "upload_anyway"] | None = Form(default=None),
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

    Epic 3 (M05-E03) Phase C2 — if the file is byte-identical to one
    already in the system and `resolution` isn't supplied, this returns
    HTTP 409 with a DuplicateFileWarningResponse instead (raised as
    DuplicateResumeFileException and handled by the existing global
    ResumeException handler — no special-casing needed here).
    """
    try:
        validated = ResumeUploadRequest(
            campaign_id=campaign_id,
            candidate_full_name=candidate_full_name,
            candidate_email=candidate_email,
            candidate_phone=candidate_phone,
            jurisdiction=jurisdiction,
            consent_confirmed=consent_confirmed,
            resolution=resolution,
        )
    except ValidationError as exc:
        raise BadRequestError(str(exc)) from exc

    file_bytes = file.file.read()
    filename = file.filename or "resume"

    resume, campaign_candidate, campaign, task_id, requires_processing = service.upload_resume(
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
        resolution=validated.resolution,
    )

    masked_name = validated.candidate_full_name.split(" ")[0]
    message = (
        "Resume uploaded successfully and queued for processing."
        if requires_processing
        else "Existing resume linked to the campaign — no reprocessing needed."
    )

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
        message=message,
    )


@router.get(
    "",
    response_model=APIResponse[ResumeListResponse],
    status_code=status.HTTP_200_OK,
)
def list_resumes(
    campaign_id: UUID | None = Query(default=None),
    parse_status: ParseStatus | None = Query(default=None),
    source: Literal["individual", "bulk"] | None = Query(default=None),
    email_hash: str | None = Query(default=None),
    uploaded_from: datetime | None = Query(default=None),
    uploaded_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["created_at", "parse_status"] = Query(default="created_at"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Read-only monitoring endpoint — paginated, filterable resume list across both individual and bulk upload sources."""
    return APIResponse.ok(
        data=service.list_resumes(
            campaign_id=campaign_id,
            parse_status=parse_status,
            source=source,
            email_hash=email_hash,
            uploaded_from=uploaded_from,
            uploaded_to=uploaded_to,
            page=page,
            size=size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        ),
        message="Resume list retrieved successfully.",
    )


@router.get(
    "/pipeline-status",
    response_model=APIResponse[ResumeListWithPipelineResponse],
    status_code=status.HTTP_200_OK,
)
def list_resumes_with_pipeline_status(
    campaign_id: UUID | None = Query(default=None),
    parse_status: ParseStatus | None = Query(default=None),
    source: Literal["individual", "bulk"] | None = Query(default=None),
    email_hash: str | None = Query(default=None),
    uploaded_from: datetime | None = Query(default=None),
    uploaded_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["created_at", "parse_status"] = Query(default="created_at"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Same rows/filters/pagination as GET /resumes, plus each row's linked
    campaign_candidate pipeline_stage and decision_type/decision_source/
    decision_reason/decision_at - which stage the candidate is on, and
    whether they succeeded or failed there.
    """
    return APIResponse.ok(
        data=service.list_resumes_with_pipeline_status(
            campaign_id=campaign_id,
            parse_status=parse_status,
            source=source,
            email_hash=email_hash,
            uploaded_from=uploaded_from,
            uploaded_to=uploaded_to,
            page=page,
            size=size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        ),
        message="Resume list with pipeline status retrieved successfully.",
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


@router.get(
    "/candidate/{campaign_candidate_id}/parsed-json",
    response_model=APIResponse[ResumeParsedJsonResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_parsed_json_by_candidate(
    campaign_candidate_id: UUID,
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Read-only monitoring endpoint — returns the campaign candidate's active resume's parsed_json."""
    return APIResponse.ok(
        data=service.get_parsed_json_by_campaign_candidate(campaign_candidate_id),
        message="Parsed resume data retrieved successfully.",
    )


@router.get(
    "/candidate/{candidate_id}/versions",
    response_model=APIResponse[ResumeVersionHistoryResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_version_history(
    candidate_id: UUID,
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Epic 3 (M05-E03) Phase C1 — read-only. Full resume version history for a candidate, most recent first, with the active version marked."""
    return APIResponse.ok(
        data=service.get_version_history(candidate_id),
        message="Resume version history retrieved successfully.",
    )


@router.get(
    "/compare",
    response_model=APIResponse[ResumeVersionComparisonResponse],
    status_code=status.HTTP_200_OK,
)
def compare_resume_versions(
    resume_id_1: UUID = Query(...),
    resume_id_2: UUID = Query(...),
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    S02-T02 — read-only diff of two resume versions belonging to the same
    candidate: skills added/removed/unchanged, experience added/removed
    (matched by title+company), education added/removed, and the
    total_experience_years difference. Computed at query time; nothing is
    persisted. Registered ahead of /{resume_id} so this literal path isn't
    shadowed by that catch-all.
    """
    return APIResponse.ok(
        data=service.compare_resume_versions(resume_id_1, resume_id_2),
        message="Resume versions compared successfully.",
    )


@router.get(
    "/{resume_id}/download-url",
    response_model=APIResponse[ResumeDownloadUrlResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_download_url(
    resume_id: UUID,
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    S02-T01 — server-generated, time-limited signed URL to download one
    specific resume version's stored file. HR_ADMIN only. Expiry is
    config-driven via RESUME_DOWNLOAD_URL_EXPIRY_SECONDS (default 300s).
    """
    return APIResponse.ok(
        data=service.get_download_url(resume_id),
        message="Resume download URL generated successfully.",
    )


@router.get(
    "/{resume_id}",
    response_model=APIResponse[ResumeDetailResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_detail(
    resume_id: UUID,
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Read-only monitoring endpoint — resume metadata, candidate summary, current processing state, skill/embedding/parser info, and failure detail if applicable."""
    return APIResponse.ok(
        data=service.get_resume_detail(resume_id),
        message="Resume detail retrieved successfully.",
    )


@router.get(
    "/{resume_id}/timeline",
    response_model=APIResponse[ResumeTimelineResponse],
    status_code=status.HTTP_200_OK,
)
def get_resume_timeline(
    resume_id: UUID,
    attempt_number: int | None = Query(default=None, ge=1),
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Read-only monitoring endpoint — per-stage execution timeline for this
    resume's processing task, resolved via resumes.task_id (stable across
    retries, set at enqueue time). Defaults to the current/latest attempt;
    pass attempt_number to view a specific historical retry instead.
    """
    return APIResponse.ok(
        data=service.get_timeline(resume_id, attempt_number=attempt_number),
        message="Resume timeline retrieved successfully.",
    )


@router.get(
    "/{resume_id}/parse-attempts",
    response_model=APIResponse[list[ParseAttemptItem]],
    status_code=status.HTTP_200_OK,
)
def get_resume_parse_attempts(
    resume_id: UUID,
    service: ResumeMonitoringService = Depends(get_resume_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Read-only monitoring endpoint — full attempt/failure history, merging resume_parse_attempts (successes) with stage_failure_logs (failures, including ones that never reached a successful attempt)."""
    return APIResponse.ok(
        data=service.get_parse_attempts(resume_id),
        message="Parse attempt history retrieved successfully.",
    )


@router.post(
    "/{resume_id}/retry",
    response_model=APIResponse[ResumeRetryResponse],
    status_code=status.HTTP_200_OK,
)
def retry_resume_parse(
    resume_id: UUID,
    service: ResumeUploadService = Depends(get_resume_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Epic 4 (M05-E04) Phase D10 - re-dispatches a FAILED resume's
    processing from its existing, already-stored file (no re-upload).
    HR_ADMIN only, matching the equivalent bulk-file replay route's
    strictness.
    """
    resume, new_task_id = service.retry_parse(
        resume_id, actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(
        data=ResumeRetryResponse(
            resume_id=resume.id, task_id=new_task_id, parse_status=resume.parse_status.value,
        ),
        message="Resume retry enqueued.",
    )


@router.post(
    "/dead-letter-queue/{dlq_id}/replay",
    response_model=APIResponse[ResumeRetryResponse],
    status_code=status.HTTP_200_OK,
)
def replay_resume_dlq_entry(
    dlq_id: UUID,
    service: ResumeUploadService = Depends(get_resume_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Epic 4 (M05-E04) Phase D10 - replays a dead-lettered (retries
    exhausted) resume-processing failure from its DLQ entry. HR_ADMIN
    only, matching the equivalent bulk-file replay route's strictness.
    """
    resume, new_task_id = service.replay_from_dlq(
        dlq_id, actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(
        data=ResumeRetryResponse(
            resume_id=resume.id, task_id=new_task_id, parse_status=resume.parse_status.value,
        ),
        message="Resume DLQ entry replay enqueued.",
    )


@router.delete(
    "/{resume_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
    summary="Delete a single resume (cleanup)",
    description=(
        "Permanently removes one resume version and everything that "
        "references it - its own campaign_candidates links (across every "
        "campaign it was linked to), stage history, skills, embeddings, "
        "parse attempts, task/DLQ history, and the stored file - without "
        "touching the candidate itself or their other resume versions. "
        "Intended for cleaning up stuck/orphaned resumes (e.g. a "
        "processing task that was enqueued but never picked up). "
        "HR_ADMIN or RECRUITER."
    ),
)
def delete_resume(
    resume_id: UUID,
    reason: str | None = Query(default=None, max_length=500),
    service: ResumeCleanupService = Depends(get_resume_cleanup_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    service.delete_resume(
        resume_id=resume_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        reason=reason,
    )

    return APIResponse.ok(
        data=None,
        message="Resume deleted successfully.",
    )
