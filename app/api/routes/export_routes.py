"""
Export endpoints.

A separate router from campaign_routes/campaign_candidate for the same reason
candidate_actions_routes is: those modules belong to other epics and have been
overwritten by pulls, so M11's surface stays in its own file.
"""
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status
from fastapi.responses import StreamingResponse

from app.dependencies.export import get_export_service, get_scheduled_export_service
from app.enums.constants import ActionType
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.export.export_schema import (
    BatchScorecardRequest,
    DsarRequest,
    ExportDispatchResponse,
    ExportPreviewResponse,
    ScheduledExportConfigRequest,
    ScheduledExportConfigResponse,
    ScheduledExportHistoryEntry,
    ScheduledExportPauseRequest,
)
from app.schemas.response import APIResponse
from app.services.export.export_service import ExportService
from app.services.export.scheduled_export_service import ScheduledExportService

router = APIRouter(prefix="/exports", tags=["Exports"])

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file(content: bytes, filename: str, media_type: str) -> StreamingResponse:
    from io import BytesIO

    # filename* (RFC 5987) as well as filename: campaign names contain spaces
    # and non-ASCII, which a bare filename= silently mangles.
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


# ── candidate list ────────────────────────────────────────────────────


@router.get(
    "/campaigns/{campaign_id}/candidates/preview",
    response_model=APIResponse[ExportPreviewResponse],
    summary="Check whether an export will run inline or in the background",
    description=(
        "Lets the export dialog tell the user what will happen before "
        "they commit, rather than discovering it after pressing Export."
    ),
)
def preview_candidate_export(
    campaign_id: UUID,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    count = export_service.candidate_list_row_count(campaign_id)
    threshold = export_service.async_threshold()
    return APIResponse.ok(data=ExportPreviewResponse(
        row_count=count,
        threshold=threshold,
        will_be_async=count > threshold,
        notify_email_hint=user.email if hasattr(user, "email") else None,
    ))


@router.get(
    "/campaigns/{campaign_id}/candidates",
    summary="Export the ranked candidate list as XLSX",
    description=(
        "The filtered list in ranking order, with an optional "
        "Rejected Candidates sheet. No PII: candidate UUID only. Above "
        "EXPORT_ASYNC_THRESHOLD rows the export is dispatched to Celery instead and this "
        "returns a task handle rather than a file."
    ),
)
def export_candidate_list(
    campaign_id: UUID,
    include_rejected_sheet: bool = Query(default=False),
    campaign_candidate_ids: list[UUID] | None = Query(default=None),
    force_sync: bool = Query(default=False, description="Bypass the async threshold."),
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    count = export_service.candidate_list_row_count(
        campaign_id, campaign_candidate_ids=campaign_candidate_ids,
    )
    threshold = export_service.async_threshold()

    if count > threshold and not force_sync:
        from app.tasks.export_tasks import generate_export_task

        task = generate_export_task.delay(
            str(campaign_id),
            kind="CANDIDATE_LIST",
            requested_by=user.user_id,
            options={
                "include_rejected_sheet": include_rejected_sheet,
                "campaign_candidate_ids": [str(i) for i in (campaign_candidate_ids or [])],
                "actor_role": user.roles[0] if user.roles else None,
            },
        )
        return APIResponse.ok(
            data=ExportDispatchResponse(
                synchronous=False,
                task_id=str(task.id),
                row_count=count,
                threshold=threshold,
                detail=(
                    f"This export contains {count} candidates. It is being generated in the "
                    "background and a download link will be available when it is ready."
                ),
            ),
            message="Export queued.",
        )

    content = export_service.build_candidate_list_xlsx(
        campaign_id,
        campaign_candidate_ids=campaign_candidate_ids,
        include_rejected_sheet=include_rejected_sheet,
    )
    export_service.log_export(
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        campaign_id=campaign_id,
        action_type=ActionType.CANDIDATE_LIST_EXPORTED.value,
        details={"title": "Candidate list exported", "rows": count, "async": False},
    )
    return _file(content, f"candidates_{_stamp()}.xlsx", XLSX_TYPE)


# ── scorecards ────────────────────────────────────────────────────────


@router.get(
    "/campaigns/{campaign_id}/candidates/{campaign_candidate_id}/scorecard",
    summary="Export one candidate's full scorecard as PDF",
    description="HR_ADMIN only. No PII: candidate UUID only.",
)
def export_scorecard(
    campaign_id: UUID,
    campaign_candidate_id: UUID,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content = export_service.build_scorecard_pdf(campaign_id, campaign_candidate_id)
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=campaign_id,
        action_type=ActionType.SCORECARD_EXPORTED.value,
        details={"title": "Scorecard exported", "campaign_candidate_id": str(campaign_candidate_id)},
    )
    return _file(content, f"scorecard_{_stamp()}.pdf", "application/pdf")


@router.post(
    "/campaigns/{campaign_id}/scorecards/batch",
    summary="Batch export candidate scorecards",
    description=(
        "2 to MAX_BATCH_SCORECARD_EXPORT candidates, as one concatenated "
        "PDF or a ZIP of individual PDFs. HR_ADMIN only."
    ),
)
def export_batch_scorecards(
    campaign_id: UUID,
    request: BatchScorecardRequest,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content, ext, ctype = export_service.build_batch_scorecards(
        campaign_id, request.campaign_candidate_ids, fmt=request.format,
    )
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=campaign_id,
        action_type=ActionType.SCORECARD_EXPORTED.value,
        details={
            "title": "Batch scorecards exported",
            "count": len(request.campaign_candidate_ids),
            "format": request.format,
        },
    )
    return _file(content, f"scorecards_{_stamp()}.{ext}", ctype)


@router.get(
    "/campaigns/{campaign_id}/shortlist-package",
    summary="Generate the hiring-manager shortlist package",
    description=(
        "Cover page, ranking summary and a scorecard page per SHORTLISTED "
        "candidate, in rank order. HR_ADMIN only."
    ),
)
def export_shortlist_package(
    campaign_id: UUID,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content = export_service.build_shortlist_package(campaign_id)
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=campaign_id,
        action_type=ActionType.SHORTLIST_PACKAGE_EXPORTED.value,
        details={"title": "Shortlist package generated"},
    )
    return _file(content, f"shortlist_package_{_stamp()}.pdf", "application/pdf")


# ── scheduled exports ─────────────────────────────────────────────────


@router.get(
    "/campaigns/{campaign_id}/schedule",
    response_model=APIResponse[ScheduledExportConfigResponse],
    summary="Read a campaign's scheduled export configuration",
    description="HR_ADMIN only.",
)
def get_schedule(
    campaign_id: UUID,
    service: ScheduledExportService = Depends(get_scheduled_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.describe(campaign_id))


@router.put(
    "/campaigns/{campaign_id}/schedule",
    response_model=APIResponse[ScheduledExportConfigResponse],
    summary="Configure a recurring ranked-list export",
    description=(
        "Frequency, time, top N, format and recipients. A schedule on a "
        "campaign that is not ACTIVE is auto-suspended until it becomes active again."
    ),
)
def put_schedule(
    campaign_id: UUID,
    request: ScheduledExportConfigRequest,
    service: ScheduledExportService = Depends(get_scheduled_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.configure(
        campaign_id,
        {**request.model_dump(), "recipients": [str(r) for r in request.recipients]},
        actor_id=user.user_id,
        actor_role=UserRole.HR_ADMIN.value,
    )
    return APIResponse.ok(data=result, message="Scheduled export saved.")


@router.post(
    "/campaigns/{campaign_id}/schedule/pause",
    response_model=APIResponse[ScheduledExportConfigResponse],
    summary="Pause or resume a scheduled export",
    description="HR_ADMIN only.",
)
def pause_schedule(
    campaign_id: UUID,
    request: ScheduledExportPauseRequest,
    service: ScheduledExportService = Depends(get_scheduled_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.set_paused(
        campaign_id, request.paused,
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value,
    )
    return APIResponse.ok(
        data=result,
        message="Scheduled export paused." if request.paused else "Scheduled export resumed.",
    )


@router.delete(
    "/campaigns/{campaign_id}/schedule",
    response_model=APIResponse[dict],
    summary="Disable a campaign's scheduled export",
    description="HR_ADMIN only.",
)
def delete_schedule(
    campaign_id: UUID,
    service: ScheduledExportService = Depends(get_scheduled_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.disable(
        campaign_id, actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value,
    )
    return APIResponse.ok(data=result, message="Scheduled export disabled.")


@router.get(
    "/campaigns/{campaign_id}/schedule/history",
    response_model=APIResponse[list[ScheduledExportHistoryEntry]],
    summary="View scheduled export history and delivery status",
    description=(
        "The last N EXPORT_GENERATE runs for this campaign. Read-only; "
        "past exports are never regenerated from here."
    ),
)
def schedule_history(
    campaign_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    service: ScheduledExportService = Depends(get_scheduled_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.history(campaign_id, limit=limit))


# ── audit & compliance ────────────────────────────────────────────────


@router.get(
    "/campaigns/{campaign_id}/audit-trail",
    summary="Export the full campaign audit trail as XLSX",
    description=(
        "All Events, Stage Transitions and Score History sheets. "
        "HR_ADMIN only."
    ),
)
def export_audit_trail(
    campaign_id: UUID,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content = export_service.build_audit_trail_xlsx(campaign_id)
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=campaign_id,
        action_type=ActionType.AUDIT_TRAIL_EXPORTED.value,
        details={"title": "Audit trail exported"},
    )
    return _file(content, f"audit_trail_{_stamp()}.xlsx", XLSX_TYPE)


@router.get(
    "/campaigns/{campaign_id}/compliance-summary",
    summary="Export the equal-opportunity compliance summary as PDF",
    description=(
        "Aggregate figures only. Contains no candidate-identifying "
        "information and no individual reviewer names. HR_ADMIN only."
    ),
)
def export_compliance_summary(
    campaign_id: UUID,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content = export_service.build_compliance_pdf(campaign_id)
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=campaign_id,
        action_type=ActionType.COMPLIANCE_REPORT_EXPORTED.value,
        details={"title": "Compliance summary exported"},
    )
    return _file(content, f"compliance_summary_{_stamp()}.pdf", "application/pdf")


@router.post(
    "/dsar",
    status_code=status.HTTP_200_OK,
    summary="Generate a Data Subject Access Report for a candidate",
    description=(
        "Looks the candidate up by email hash and returns every record "
        "held about them across all campaigns. The submitted email is used only to compute "
        "the hash and is never stored. HR_ADMIN only."
    ),
)
def export_dsar(
    request: DsarRequest,
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    content, candidate_id = export_service.build_dsar_xlsx(str(request.email))
    export_service.log_export(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value, campaign_id=None,
        action_type=ActionType.DSAR_EXPORTED.value,
        # The subject is recorded by id; the email that was searched is not.
        details={"title": "DSAR generated", "candidate_id": str(candidate_id)},
    )
    return _file(content, f"dsar_{candidate_id}_{_stamp()}.xlsx", XLSX_TYPE)
