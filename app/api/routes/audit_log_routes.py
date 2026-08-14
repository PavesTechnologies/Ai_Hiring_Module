from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status
from fastapi.responses import StreamingResponse

from app.dependencies.campaign import get_audit_service
from app.enums.constants import ActionType, DEFAULT_PAGE_SIZE, EntityType, MAX_PAGE_SIZE
from app.middleware.rbac import require_roles
from app.models.identity import UserRole
from app.schemas.audit.audit_log_schema import AuditLogSearchResponse
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService

router = APIRouter(
    prefix="/audit-log",
    tags=["Audit Log"],
)


@router.get(
    "",
    response_model=APIResponse[AuditLogSearchResponse],
    status_code=status.HTTP_200_OK,
    summary="Search Audit Log",
    description=(
        "Epic 3 Fix 5: global, cross-entity audit log search. audit_log only - "
        "does not merge in campaign_candidate_stage_history/bulk_upload_job like "
        "GET /campaigns/{id}/timeline does (that's a single-campaign activity feed "
        "with its own dedicated view). HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def search_audit_log(
    entity_type: EntityType | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    campaign_id: UUID | None = Query(default=None),
    action_type: ActionType | None = Query(default=None),
    created_from: datetime | None = Query(default=None, description="Inclusive lower bound on created_at."),
    created_to: datetime | None = Query(default=None, description="Inclusive upper bound on created_at."),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: AuditService = Depends(get_audit_service),
):
    result = service.search_audit_log(
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        campaign_id=campaign_id,
        action_type=action_type,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )

    return APIResponse.ok(
        data=result,
        message="Audit log entries retrieved successfully.",
    )


@router.get(
    "/export",
    status_code=status.HTTP_200_OK,
    summary="Export Audit Log As CSV",
    description=(
        "Same filters as GET /audit-log, no row cap - streams the full filtered "
        "result set as CSV using keyset pagination, never materializing it all in "
        "memory at once. `detail` is a raw JSON string per row (its shape varies "
        "too much by action_type to flatten into columns). HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def export_audit_log(
    entity_type: EntityType | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    campaign_id: UUID | None = Query(default=None),
    action_type: ActionType | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    service: AuditService = Depends(get_audit_service),
) -> StreamingResponse:
    filename = f"audit_log_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    return StreamingResponse(
        service.export_audit_log_csv(
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
            campaign_id=campaign_id,
            action_type=action_type,
            created_from=created_from,
            created_to=created_to,
        ),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
