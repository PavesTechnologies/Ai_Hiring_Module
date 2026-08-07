from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    active_resume_version: int | None = None
    uploaded_at: datetime | None = None
    parse_status: ParseStatus | None = None


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


class AddCandidateToCampaignRequest(BaseModel):
    campaign_id: UUID

    model_config = ConfigDict(from_attributes=True)


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
