from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage


class CandidateInfoResponse(BaseModel):
    candidate_id: UUID
    full_name: str | None = None
    # Masked, never the raw decrypted address — see
    # TalentPoolService._mask_email.
    email: str | None = None
    designation: str | None = None
    experience: float | None = None
    location: str | None = None
    jurisdiction: str


class ConsentInfoResponse(BaseModel):
    consent_given: bool
    consent_timestamp: datetime | None = None
    consent_version: str | None = None


class TalentPoolInfoResponse(BaseModel):
    is_talent_pool_eligible: bool
    # resume_embeddings has no dedicated "updated_at" column — this is the
    # created_at of the active resume version's embedding row, i.e. when
    # that embedding was last (re)generated.
    embedding_updated_at: datetime | None = None


class ResumeInfoResponse(BaseModel):
    # Added for the Talent Pool candidate profile page's Download action -
    # active_resume_version alone (an int) isn't enough to call
    # GET /resumes/{resume_id}/download-url.
    resume_id: UUID | None = None
    active_resume_version: int | None = None
    uploaded_at: datetime | None = None
    parse_status: ParseStatus | None = None
    # Profile page's Summary tab - the active resume's own parsed_json.summary,
    # read the exact same way TalentPoolSearchItem.summary is (never
    # generated, never JD-specific). A single-candidate read, so unlike the
    # search endpoint this needs no batching.
    summary: str | None = None


class CampaignSummaryResponse(BaseModel):
    total_campaigns: int
    latest_campaign: str | None = None
    latest_pipeline_stage: PipelineStage | None = None


class PerformanceSummaryResponse(BaseModel):
    best_composite_score: float | None = None
    campaign_name: str | None = None
    jd_title: str | None = None
    average_composite_score: float | None = None
    # best_ai_recommendation intentionally omitted - campaign_candidates
    # has no ai_recommendation column on the live database, and AI
    # evaluation results are never persisted anywhere in this codebase
    # today (see TalentPoolService._queue_evaluation_tasks).
    shortlisted_count: int
    selected_count: int
    total_campaigns: int
    top_5_skills: list[str] = Field(default_factory=list)


class TalentPoolCandidateProfileResponse(BaseModel):
    candidate: CandidateInfoResponse
    consent: ConsentInfoResponse
    talent_pool: TalentPoolInfoResponse
    resume: ResumeInfoResponse
    campaign_summary: CampaignSummaryResponse
    performance_summary: PerformanceSummaryResponse


class TalentPoolSearchItem(BaseModel):
    candidate: CandidateInfoResponse
    # Informational only - NOT a selection. ResumeSelectionService
    # independently determines which resume version is actually used when
    # this candidate is added to a campaign, and may choose a different
    # eligible version if the candidate has more than one.
    matching_resume_id: UUID
    matching_resume_version: int
    # Card enrichment (M13-E01 S02 T0x) - all read directly off data already
    # fetched for this search, never generated and never JD-specific:
    # summary is matching_resume's own parsed_json.summary (the same field
    # _extract_resume_fields already reads designation/experience/location
    # from); skills are every canonical candidate_skills row for that same
    # resume (batched across the page, not looped); best_composite_score is
    # MAX(campaign_candidates.composite_score) across every campaign this
    # candidate has ever been submitted to (also batched), null when no
    # campaign evaluation exists yet.
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    best_composite_score: float | None = None


class TalentPoolSearchResponse(BaseModel):
    items: list[TalentPoolSearchItem]
    total: int
    page: int
    size: int


class AddCandidateToCampaignResponse(BaseModel):
    campaign_candidate_id: UUID
    campaign_id: UUID
    candidate_id: UUID
    resume_id: UUID
    pipeline_stage: PipelineStage
    # ai_evaluation_status intentionally omitted - campaign_candidates has
    # no such column on the live database.
    # Task-type strings actually queued as a result of this call - either
    # ["RESUME_DOCUMENT_PROCESSING"] (resume re-parsed from scratch) or
    # ["SKILL_NORMALIZE", "EMBED_RESUME"] (parsing already current, only
    # skills/embedding refreshed). Empty when this call resolved to an
    # already-existing campaign_candidates row (idempotent retry).
    queued_task_types: list[str] = Field(default_factory=list)


class BulkAddCandidatesRequest(BaseModel):
    """Talent Pool Search -> select multiple candidates -> add them all to one campaign in a single call."""
    campaign_id: UUID
    candidate_ids: list[UUID] = Field(..., min_length=1)


class BulkAddCandidateResultItem(BaseModel):
    candidate_id: UUID
    status: Literal["ADDED", "FAILED"]
    campaign_candidate_id: UUID | None = None
    resume_id: UUID | None = None
    # Populated only when status == "FAILED" - a caller-safe message only
    # (see TalentPoolService._failure_reason), never a raw exception/traceback.
    reason: str | None = None


class BulkAddCandidatesResponse(BaseModel):
    campaign_id: UUID
    total: int
    added: int
    failed: int
    results: list[BulkAddCandidateResultItem]
