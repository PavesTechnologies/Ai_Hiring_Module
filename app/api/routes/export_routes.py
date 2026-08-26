"""
Export endpoints.

A separate router from campaign_routes/campaign_candidate for the same reason
candidate_actions_routes is: those modules belong to other epics and have been
overwritten by pulls, so M11's surface stays in its own file.
"""
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security
from fastapi.responses import StreamingResponse

from app.dependencies.export import get_export_service
from app.enums.constants import ActionType
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.export.export_schema import ExportDispatchResponse
from app.schemas.response import APIResponse
from app.services.export.export_service import ExportService

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
    export_service: ExportService = Depends(get_export_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    count = export_service.candidate_list_row_count(
        campaign_id, campaign_candidate_ids=campaign_candidate_ids,
    )
    threshold = export_service.async_threshold()

    if count > threshold:
        from app.tasks.export_tasks import generate_export_task

        task = generate_export_task.delay(
            str(campaign_id),
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


# ── audit trail ───────────────────────────────────────────────────────


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
