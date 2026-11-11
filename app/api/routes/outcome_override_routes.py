"""Recruiter overrides (Phase 4): the reason codes, and the patterns report for HR_ADMIN."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.middleware.rbac import require_roles
from app.models.identity import UserRole
from app.repositories.config_repository import ConfigRepository
from app.schemas.response import APIResponse
from app.services.campaign.outcome_override_report import override_report
from app.services.campaign.outcome_overrides import reason_code_options

router = APIRouter(prefix="/outcome-overrides", tags=["Outcome Overrides"])


@router.get(
    "/reason-codes",
    response_model=APIResponse[list[dict]],
    status_code=status.HTTP_200_OK,
    summary="The reason codes a move against the system's outcome needs",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER))],
)
def list_reason_codes(db: Session = Depends(get_db)):
    return APIResponse.ok(data=reason_code_options(db), message="Override reason codes.")


@router.get(
    "/report",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Recruiter overrides: totals and the most common patterns",
    description=(
        "Moves against the system's screening outcome (OVERRIDE) and HOLD resolutions (REVIEW_DECISION): "
        "the most common patterns (kind, system outcome, direction, reason), and counts by reason, by the "
        "failed checks and missing core skills behind the overridden outcome, by template, role and "
        "campaign (with the override rate and the override-rate alert). No candidate details. HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_override_report(
    campaign_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None, description="Inclusive lower bound on when the move was made."),
    until: datetime | None = Query(default=None, description="Exclusive upper bound."),
    top: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return APIResponse.ok(data=override_report(db, campaign_id=campaign_id, since=since, until=until, top=top,
                                               config_repository=ConfigRepository(db)),
                          message="Override report.")
