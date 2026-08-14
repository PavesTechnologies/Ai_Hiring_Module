from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.talent_pool import get_talent_pool_service
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.models.pipeline import PipelineStage
from app.schemas.response import APIResponse
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignResponse,
    BulkAddCandidatesRequest,
    BulkAddCandidatesResponse,
    TalentPoolCandidateProfileResponse,
    TalentPoolFiltersResponse,
    TalentPoolSearchResponse,
    TalentPoolSemanticSearchRequest,
    TalentPoolSemanticSearchResponse,
)
from app.services.talent_pool.talent_pool_service import TalentPoolService, TALENT_POOL_MAX_PAGE_SIZE

router = APIRouter(
    prefix="/talent-pool",
    tags=["Talent Pool"],
)

# Deliberately a separate router with no "/talent-pool" prefix: the
# required path is exactly GET /talentpoolfilters (sibling to, not nested
# under, /talent-pool/...), kept in this same module since it's minimal
# supporting code for the Talent Pool Normal Search UI, not a second
# feature area.
filters_router = APIRouter(tags=["Talent Pool"])


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
        "M13-E01 S02 - Talent Pool Normal Search. `search` matches against "
        "candidate name OR skills (whitespace-separated tokens AND'd - "
        "'Python AWS' requires both skills); `skill`/`skills` remain an "
        "independent OR'd skill filter kept for backward compatibility. "
        "designation/designations and location/locations are each OR'd, "
        "case-insensitive substring, multi-select filters. degree_levels/"
        "education_fields/campaign_ids/pipeline_stages are each OR'd within "
        "their own category; every distinct filter category combines with "
        "AND. experience_min/experience_max filter total years of "
        "experience; score_min/score_max filter the same best_composite_score "
        "already shown on each card (never a semantic/AI score). campaign_id "
        "(singular), when given, excludes candidates already added to that "
        "campaign — the 'who's left to add' view — independent of the "
        "inclusion-based campaign_ids. Every filter is applied at the "
        "PostgreSQL level (WHERE/EXISTS/JOIN), never by loading the Talent "
        "Pool into application memory. Page size is capped at "
        f"{TALENT_POOL_MAX_PAGE_SIZE} regardless of what is requested. Only "
        "candidates with at least one eligible resume (PARSED, has an "
        "embedding, is_talent_pool_eligible, and fresh per "
        "RESUME_FRESHNESS_MAX_AGE_DAYS) are returned. Read-only - no resume "
        "is selected here; ResumeSelectionService independently selects the "
        "resume actually used once a candidate is added to a campaign. "
        "HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def search_talent_pool_candidates(
    search: str | None = Query(
        default=None, min_length=1, max_length=255,
        description="Normal Search - matches candidate name OR skills (space-separated tokens AND'd).",
    ),
    skill: str | None = Query(default=None, min_length=1, max_length=255),
    skills: list[str] | None = Query(default=None, description="Repeat for multiple — ?skills=Java&skills=AWS"),
    designation: str | None = Query(default=None, min_length=1, max_length=255),
    designations: list[str] | None = Query(
        default=None, description="Repeat for multiple — ?designations=Python Developer&designations=Java Developer",
    ),
    location: str | None = Query(default=None, min_length=1, max_length=255),
    locations: list[str] | None = Query(
        default=None, description="Repeat for multiple — ?locations=Bengaluru&locations=Austin",
    ),
    degree_levels: list[str] | None = Query(default=None, description="Repeat for multiple — OR'd together."),
    education_fields: list[str] | None = Query(default=None, description="Repeat for multiple — OR'd together."),
    campaign_ids: list[UUID] | None = Query(
        default=None, description="Only candidates already in one of these campaigns — OR'd together.",
    ),
    pipeline_stages: list[PipelineStage] | None = Query(
        default=None, description="Repeat for multiple — OR'd together.",
    ),
    experience_min: float | None = Query(default=None, ge=0, description="Minimum total years of experience."),
    experience_max: float | None = Query(default=None, ge=0, description="Maximum total years of experience."),
    score_min: float | None = Query(default=None, ge=0, le=100, description="Minimum best_composite_score."),
    score_max: float | None = Query(default=None, ge=0, le=100, description="Maximum best_composite_score."),
    campaign_id: UUID | None = Query(
        default=None, description="Exclude candidates already added to this campaign",
    ),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=6, ge=1, le=100, description=f"Capped server-side at {TALENT_POOL_MAX_PAGE_SIZE}."),
    service: TalentPoolService = Depends(get_talent_pool_service),
):
    return APIResponse.ok(
        data=service.search_candidates(
            search=search, skill=skill, skills=skills,
            designation=designation, designations=designations,
            location=location, locations=locations,
            degree_levels=degree_levels, education_fields=education_fields,
            campaign_ids=campaign_ids, pipeline_stages=pipeline_stages,
            experience_min=experience_min, experience_max=experience_max,
            score_min=score_min, score_max=score_max,
            campaign_id=campaign_id, page=page, size=size,
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


@router.post(
    "/semantic-search",
    response_model=APIResponse[TalentPoolSemanticSearchResponse],
    status_code=status.HTTP_200_OK,
    summary="Semantic Search Talent Pool Candidates",
    description=(
        "M14 - Talent Pool Semantic Search. `query` is a free-text passage "
        "- a full resume, a candidate profile, a job description, a role "
        "description, a recruiter requirement, or any other meaningful "
        "candidate-related text - embedded as one whole meaning-bearing "
        "text exactly once per request, never split into skill tokens or "
        "matched with Normal Search's `search`-box rules. Optional "
        "structured `filters` use the exact same semantics as Normal "
        "Search's own filter categories (OR within a category, AND across "
        "categories) and are applied FIRST, entirely in PostgreSQL, to "
        "establish the eligible/filtered candidate set - vector similarity "
        "ranking then runs ONLY within that filtered set, never across the "
        "whole Talent Pool followed by a Python-side filter. Ranked by "
        "pgvector cosine similarity against the existing resume embedding "
        "for each candidate's single eligible resume (same Talent Pool "
        "eligibility and one-resume-per-candidate rules as Normal Search); "
        "candidate embeddings are read as-is, never regenerated here. "
        "`total` reflects the structured-filtered, eligible candidate "
        "count, independent of the current page. Page size is capped at "
        f"{TALENT_POOL_MAX_PAGE_SIZE} regardless of what is requested. "
        "HR_ADMIN, RECRUITER, and HIRING_MANAGER only."
    ),
    dependencies=[
        Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    ],
)
def semantic_search_talent_pool_candidates(
    request: TalentPoolSemanticSearchRequest,
    service: TalentPoolService = Depends(get_talent_pool_service),
):
    return APIResponse.ok(
        data=service.semantic_search_candidates(
            query=request.query,
            filters=request.filters,
            page=request.page,
            size=request.size,
        ),
        message="Talent Pool semantic search results retrieved successfully.",
    )


@filters_router.get(
    "/talentpoolfilters",
    response_model=APIResponse[TalentPoolFiltersResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Talent Pool Search Filter Options",
    description=(
        "Filter option metadata for the Talent Pool Normal Search UI: "
        "distinct candidate locations and designations (case-insensitive "
        "deduped from parsed resume data), education degree_level/"
        "field_normalized values already classified by the resume-"
        "extraction pipeline, active campaigns (id + name), and the "
        "existing PipelineStage values. Read-only - never executes a "
        "candidate search. HR_ADMIN, RECRUITER, and HIRING_MANAGER only."
    ),
    dependencies=[
        Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    ],
)
def get_talent_pool_filters(
    service: TalentPoolService = Depends(get_talent_pool_service),
):
    return APIResponse.ok(
        data=service.get_search_filters(),
        message="Talent Pool filter options retrieved successfully.",
    )
