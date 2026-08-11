from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.talent_pool import get_talent_pool_service
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignResponse,
    BulkAddCandidatesRequest,
    BulkAddCandidatesResponse,
    TalentPoolCandidateProfileResponse,
    TalentPoolSearchResponse,
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


def _parse_campaign_id(campaign_id: str) -> UUID:
    try:
        return UUID(campaign_id)
    except ValueError as exc:
        raise BadRequestError("Invalid campaign_id — must be a valid UUID.") from exc


@router.get(
    "/candidates",
    response_model=APIResponse[TalentPoolSearchResponse],
    status_code=status.HTTP_200_OK,
    summary="Search / Filter Talent Pool Candidates",
    description=(
        "M13-E01 S02 - search candidates in the Talent Pool, optionally "
        "filtered by one or more skills (each matched against canonical "
        "skill name/alias and raw extracted skill text, OR'd together — a "
        "candidate matching ANY listed skill is included), a "
        "case-insensitive designation substring, and/or a campaign_id (when "
        "given, excludes candidates already added to that campaign — the "
        "'who's left to add' view). Only candidates with at least one "
        "eligible resume (PARSED, has an embedding, is_talent_pool_eligible, "
        "and fresh per RESUME_FRESHNESS_MAX_AGE_DAYS) are returned. "
        "Read-only - no resume is selected here; ResumeSelectionService "
        "independently selects the resume actually used once a candidate "
        "is added to a campaign. HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def search_talent_pool_candidates(
    skill: str | None = Query(default=None, min_length=1, max_length=255),
    skills: list[str] | None = Query(default=None, description="Repeat for multiple — ?skills=Java&skills=AWS"),
    designation: str | None = Query(default=None, min_length=1, max_length=255),
    campaign_id: UUID | None = Query(
        default=None, description="Exclude candidates already added to this campaign",
    ),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    service: TalentPoolService = Depends(get_talent_pool_service),
):
    return APIResponse.ok(
        data=service.search_candidates(
            skill=skill, skills=skills, designation=designation, campaign_id=campaign_id, page=page, size=size,
        ),
        message="Talent Pool candidates retrieved successfully.",
    )


@router.post(
    "/candidates/bulk-add-to-campaign",
    response_model=APIResponse[BulkAddCandidatesResponse],
    status_code=status.HTTP_200_OK,
    summary="Bulk Add Talent Pool Candidates to a New Campaign",
    description=(
        "Talent Pool Search -> select multiple candidates -> add them all "
        "to one ACTIVE campaign in a single call. Each candidate is added "
        "via the exact same logic as the single-candidate add-to-campaign "
        "endpoint (same eligibility checks, same ResumeSelectionService "
        "selection), independently - one candidate's failure (already in "
        "campaign, no eligible resume, etc.) does not block or roll back "
        "any other candidate's already-committed add. HR_ADMIN only."
    ),
)
def bulk_add_talent_pool_candidates_to_campaign(
    request: BulkAddCandidatesRequest,
    service: TalentPoolService = Depends(get_talent_pool_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.bulk_add_candidates_to_campaign(
        request.candidate_ids,
        request.campaign_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=result,
        message=f"{result.added} candidate(s) added, {result.failed} failed.",
    )


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
    "/candidates/{candidate_id}/campaigns/{campaign_id}",
    response_model=APIResponse[AddCandidateToCampaignResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Add Talent Pool Candidate to a Campaign",
    description=(
        "Adds a Talent Pool candidate to the given ACTIVE campaign. "
        "Validates the candidate and campaign, ensures the candidate isn't "
        "already in the campaign, then delegates entirely to "
        "TalentPoolService.add_candidate_to_campaign - resume selection is "
        "never performed in this route; ResumeSelectionService picks the "
        "best eligible resume version for this specific campaign, stored "
        "on campaign_candidates.resume_id. Idempotent retries, audit "
        "logging, and Celery evaluation-task dispatch are unchanged from "
        "the existing add-to-campaign flow. HR_ADMIN only."
    ),
)
def add_talent_pool_candidate_to_campaign(
    candidate_id: str,
    campaign_id: str,
    service: TalentPoolService = Depends(get_talent_pool_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.add_candidate_to_campaign(
        _parse_candidate_id(candidate_id),
        _parse_campaign_id(campaign_id),
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=result,
        message="Candidate added to campaign successfully.",
    )
