from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.talent_pool import get_talent_pool_service
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignRequest,
    AddCandidateToCampaignResponse,
    TalentPoolCandidateProfileResponse,
)
from app.services.talent_pool.talent_pool_service import TalentPoolService

router = APIRouter(
    prefix="/talent-pool",
    tags=["Talent Pool"],
)


def _parse_candidate_id(candidate_id: str) -> UUID:
    try:
        return UUID(candidate_id)
    except ValueError as exc:
        raise BadRequestError("Invalid candidate_id — must be a valid UUID.") from exc


@router.get(
    "/candidates/{candidate_id}",
    response_model=APIResponse[TalentPoolCandidateProfileResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Unified Candidate Profile",
    description=(
        "M13-E01 S01 - the candidate's unified profile across every campaign "
        "they have ever been submitted to: identity, consent, talent-pool "
        "eligibility, active resume, and cross-campaign performance summary "
        "(best/average composite score, best AI recommendation, "
        "shortlisted/selected counts, top 5 skills). HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_talent_pool_candidate_profile(
    candidate_id: str,
    service: TalentPoolService = Depends(get_talent_pool_service),
):
    profile = service.get_candidate_profile(_parse_candidate_id(candidate_id))

    return APIResponse.ok(
        data=profile,
        message="Candidate profile retrieved successfully.",
    )


@router.post(
    "/candidates/{candidate_id}/add-to-campaign",
    response_model=APIResponse[AddCandidateToCampaignResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Add Talent Pool Candidate to a New Campaign",
    description=(
        "M13-E01 S01 - adds an existing talent-pool candidate directly to "
        "another ACTIVE campaign, reusing their current active resume. "
        "Re-queues resume parsing if outdated, otherwise refreshes skill "
        "normalization and the resume embedding. HR_ADMIN only."
    ),
)
def add_talent_pool_candidate_to_campaign(
    candidate_id: str,
    request: AddCandidateToCampaignRequest,
    service: TalentPoolService = Depends(get_talent_pool_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.add_candidate_to_campaign(
        _parse_candidate_id(candidate_id),
        request.campaign_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=result,
        message="Candidate added to campaign successfully.",
    )
