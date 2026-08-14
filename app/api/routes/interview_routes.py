from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.campaign_candidate import get_interview_schedule_service
from app.middleware.rbac import TokenUser, get_current_user, require_roles
from app.models.identity import UserRole
from app.schemas.interview_schema import (
    CancelInterviewRequest,
    InterviewScheduleResponse,
    RescheduleInterviewRequest,
    ScheduleInterviewRequest,
)
from app.schemas.response import APIResponse
from app.services.interview_schedule_service import InterviewScheduleService

router = APIRouter(tags=["Interview Scheduling"])


@router.post(
    "/campaign-candidates/{campaign_candidate_id}/interviews",
    response_model=APIResponse[InterviewScheduleResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Schedule Interview",
    description=(
        "Fills in the PENDING interview_schedules row auto-created when the "
        "candidate reached INTERVIEW (StageTransitionService.transition()'s "
        "Epic 4 hook) - only succeeds from PENDING; use reschedule "
        "afterward. HIRING_MANAGER (own campaign only) or HR_ADMIN, "
        "matching Epic 1's advance-to-interview."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def schedule_interview(
    campaign_candidate_id: UUID,
    request: ScheduleInterviewRequest,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.schedule(
        campaign_candidate_id, actor_id=user.user_id, actor_roles=user.roles, request=request,
    )
    return APIResponse.ok(data=result, message="Interview scheduled.")


@router.patch(
    "/interviews/{interview_id}/reschedule",
    response_model=APIResponse[InterviewScheduleResponse],
    status_code=status.HTTP_200_OK,
    summary="Reschedule Interview",
    description=(
        "Only from SCHEDULED or RESCHEDULED - moves status to RESCHEDULED "
        "and appends one interview_schedule_history row. HIRING_MANAGER "
        "(own campaign only) or HR_ADMIN."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def reschedule_interview(
    interview_id: UUID,
    request: RescheduleInterviewRequest,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.reschedule(
        interview_id, actor_id=user.user_id, actor_roles=user.roles, request=request,
    )
    return APIResponse.ok(data=result, message="Interview rescheduled.")


@router.patch(
    "/interviews/{interview_id}/cancel",
    response_model=APIResponse[InterviewScheduleResponse],
    status_code=status.HTTP_200_OK,
    summary="Cancel Interview",
    description=(
        "Any non-CANCELLED status -> CANCELLED. reason is stored in "
        "cancel_reason, never notes. HIRING_MANAGER (own campaign only) or "
        "HR_ADMIN."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def cancel_interview(
    interview_id: UUID,
    request: CancelInterviewRequest,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.cancel(
        interview_id, actor_id=user.user_id, actor_roles=user.roles, request=request,
    )
    return APIResponse.ok(data=result, message="Interview cancelled.")
