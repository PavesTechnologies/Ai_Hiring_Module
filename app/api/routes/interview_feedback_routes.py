from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.interview_feedback import get_interview_feedback_service
from app.middleware.rbac import TokenUser, get_current_user, require_roles
from app.models.identity import UserRole
from app.schemas.interview_feedback_schema import (
    FeedbackFormContextResponse,
    InterviewFeedbackResponse,
    SubmitFeedbackRequest,
)
from app.schemas.response import APIResponse
from app.services.interview_feedback_service import InterviewFeedbackService

router = APIRouter(tags=["Interview Feedback"])


@router.get(
    "/interviews/feedback/{token}",
    response_model=APIResponse[FeedbackFormContextResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Feedback Form Context",
    description=(
        "Public - no login. Interviewers have no user account at all "
        "(deliberate); the signed, expiring token in the path is the "
        "entire access-control mechanism, same shape as the OAuth "
        "callback exception (see app.core.feedback_token). Returns just "
        "enough to render the form - candidate's name (decrypted, "
        "nothing else PII-bearing), round's interview_type/date/time, "
        "and the interviewer's own name (confirms identity matches the "
        "token). 404 if the token is invalid, expired, or tampered."
    ),
)
def get_feedback_form_context(
    token: str, service: InterviewFeedbackService = Depends(get_interview_feedback_service),
):
    result = service.get_feedback_form_context(token)
    return APIResponse.ok(data=result)


@router.post(
    "/interviews/feedback/{token}",
    response_model=APIResponse[None],
    status_code=status.HTTP_201_CREATED,
    summary="Submit Interview Feedback",
    description=(
        "Public - no login, same token as the GET above. Advisory only: "
        "never touches interview_schedules.status or campaign_candidates."
        "pipeline_stage - HR/HM still act via select/reject-interview. "
        "One submission per interviewer per round, hard-locked "
        "(UNIQUE(interview_schedule_id, interviewer_id)) - a second "
        "attempt is 409, not a silent update, matching every other "
        "append-only guarantee in this system. Audit-logged with "
        "actor_id=None, actor_role='EXTERNAL_INTERVIEWER' - there is no "
        "user account to attribute this to."
    ),
)
def submit_feedback(
    token: str, request: SubmitFeedbackRequest,
    service: InterviewFeedbackService = Depends(get_interview_feedback_service),
):
    service.submit_feedback(token, request)
    return APIResponse.ok(message="Feedback submitted. Thank you.")


@router.get(
    "/campaign-candidates/{campaign_candidate_id}/interviews/{interview_id}/feedback",
    response_model=APIResponse[list[InterviewFeedbackResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get Interview Feedback",
    description=(
        "Authenticated. Lets HR/HM see all feedback submitted for a "
        "specific round, to inform the real decision made via select/ "
        "reject-interview. HIRING_MANAGER (own campaign only) or "
        "HR_ADMIN, same ownership rules as every other interview "
        "endpoint."
    ),
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER, UserRole.HR_ADMIN))],
)
def get_interview_feedback(
    campaign_candidate_id: UUID, interview_id: UUID,
    service: InterviewFeedbackService = Depends(get_interview_feedback_service),
    user: TokenUser = Depends(get_current_user),
):
    result = service.get_feedback_for_round(
        campaign_candidate_id, interview_id, actor_id=user.user_id, actor_roles=user.roles,
    )
    return APIResponse.ok(data=result)
