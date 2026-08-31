from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.campaign_candidate import get_interview_schedule_service
from app.middleware.rbac import TokenUser, get_current_user, require_roles
from app.models.identity import UserRole
from app.models.interview import InterviewStatus
from app.schemas.interview_schema import (
    CampaignInterviewEntry,
    CancelInterviewRequest,
    CompleteInterviewResponse,
    InterviewScheduleResponse,
    RequestFeedbackResponse,
    RescheduleInterviewRequest,
    ScheduleInterviewRequest,
)
from app.schemas.response import APIResponse
from app.services.interview_schedule_service import InterviewScheduleService

router = APIRouter(tags=["Interview Scheduling"])


@router.get(
    "/campaigns/{campaign_id}/interviews",
    response_model=APIResponse[list[CampaignInterviewEntry]],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Interview Calendar",
    description=(
        "Read-only - every interview round across every candidate in one "
        "campaign, backing a calendar-style view. All filters are "
        "optional: start_date/end_date filter by start_at falling in "
        "that range, status filters to one or more round statuses "
        "(PENDING/SCHEDULED/RESCHEDULED/COMPLETED/CANCELLED), "
        "interviewer_email filters to rounds that person is currently an "
        "ACTIVE interviewer on - a round they were since removed from "
        "does not match. No filters supplied returns every round in the "
        "campaign; no pagination. HIRING_MANAGER (own campaigns only, "
        "via hiring_manager_id) / RECRUITER (campaigns they uploaded to "
        "or created) / HR_ADMIN (any campaign)."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.RECRUITER, UserRole.HR_ADMIN))],
)
def get_campaign_interviews(
    campaign_id: UUID,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    status_: list[InterviewStatus] | None = Query(default=None, alias="status"),
    interviewer_email: str | None = Query(default=None),
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.get_campaign_interviews(
        campaign_id, actor_id=user.user_id, actor_roles=user.roles,
        start_date=start_date, end_date=end_date, statuses=status_, interviewer_email=interviewer_email,
    )
    return APIResponse.ok(data=result)


@router.get(
    "/campaign-candidates/{campaign_candidate_id}/interviews",
    response_model=APIResponse[list[InterviewScheduleResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get Interview Rounds",
    description=(
        "Read-only. Multi-round follow-up: a candidate can now have "
        "several interview_schedules rows (one per round) - returns ALL "
        "of them, ordered by round_number ascending, as a plain list "
        "(same APIResponse[list[...]] convention as every other "
        "list-returning endpoint in this codebase - no extra "
        "{\"rounds\": [...]} wrapper). Each item is the same shape "
        "schedule/reschedule/cancel already return. 404 only if no row "
        "exists at all. HIRING_MANAGER (own campaign only) or HR_ADMIN, "
        "same as the other 3 interview endpoints."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def get_interview_rounds(
    campaign_candidate_id: UUID,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.get_rounds(campaign_candidate_id, actor_id=user.user_id, actor_roles=user.roles)
    return APIResponse.ok(data=result)


@router.post(
    "/campaign-candidates/{campaign_candidate_id}/interviews",
    response_model=APIResponse[InterviewScheduleResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Schedule Interview / Schedule Next Round",
    description=(
        "If the candidate's latest round is PENDING (the row auto-created "
        "when the candidate reached INTERVIEW - StageTransitionService."
        "transition()'s Epic 4 hook), fills it in as round 1. If the "
        "latest round is SCHEDULED/RESCHEDULED/CANCELLED, this call means "
        "'Schedule Next Round': creates round_number+1 from this request "
        "body - the previous round's own status is left as-is (rounds "
        "are closed out by the candidate leaving INTERVIEW, not by "
        "scheduling the next one). HIRING_MANAGER (own campaign only) or "
        "HR_ADMIN, matching Epic 1's advance-to-interview."
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
        "From SCHEDULED, RESCHEDULED, or CANCELLED (reactivates a "
        "cancelled interview rather than leaving it a dead end) - moves "
        "status to RESCHEDULED and appends one interview_schedule_history "
        "row. Reactivating from CANCELLED clears cancel_reason and any "
        "stale meeting_link/calendar event left over from the "
        "cancellation. HIRING_MANAGER (own campaign only) or HR_ADMIN."
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


@router.post(
    "/interviews/{interview_id}/request-feedback",
    response_model=APIResponse[RequestFeedbackResponse],
    status_code=status.HTTP_200_OK,
    summary="Request Interview Feedback",
    description=(
        "Manual counterpart to the hourly feedback-request sweep (Epic "
        "5 Step 4) - triggers INTERVIEW_FEEDBACK_REQUESTED immediately "
        "instead of waiting for end_at to be swept. 400 if the interview "
        "hasn't started yet. Queues one email per interviewer who hasn't "
        "already given feedback or already been emailed - 0 is a valid, "
        "successful result when there's nothing left to request, not an "
        "error. HIRING_MANAGER (own campaign only) or HR_ADMIN."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def request_interview_feedback(
    interview_id: UUID,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.request_feedback(interview_id, actor_id=user.user_id, actor_roles=user.roles)
    return APIResponse.ok(data=result)


@router.post(
    "/interviews/{interview_id}/complete",
    response_model=APIResponse[CompleteInterviewResponse],
    status_code=status.HTTP_200_OK,
    summary="Mark Interview Complete",
    description=(
        "Epic 5 follow-up - manually marks a round COMPLETED once its "
        "interview has actually happened (end_at passed). 400 if end_at "
        "hasn't passed yet, or if the round is already COMPLETED/"
        "CANCELLED (never a silent no-op). On success also queues an "
        "INTERVIEW_FEEDBACK_REQUESTED email for every interviewer who "
        "hasn't already given feedback or already been emailed - the "
        "same dedup logic as the hourly sweep and the manual 'Request "
        "Feedback' button, just a third caller of it. Once COMPLETED, "
        "the round can no longer be edited via reschedule. HIRING_MANAGER "
        "(own campaign only) or HR_ADMIN."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def complete_interview(
    interview_id: UUID,
    service: InterviewScheduleService = Depends(get_interview_schedule_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.complete(interview_id, actor_id=user.user_id, actor_roles=user.roles)
    return APIResponse.ok(data=result)
