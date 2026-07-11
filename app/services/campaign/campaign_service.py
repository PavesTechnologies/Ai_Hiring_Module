from datetime import datetime, timezone
from decimal import Decimal
from http.client import responses
from http.client import responses
from unicodedata import name
from uuid import UUID
from app.middleware.rbac import TokenUser

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.enums.constants import ActionType, EntityType, UserRole
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.identity import User
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.jd_repository import JDRepository
from app.schemas.campaign.campaign_response import CampaignResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest
from app.services.audit_service import AuditService
from app.schemas.campaign.campaign_detail_response import (
    CampaignDetailResponse,
    CampaignInfoSection,
    JDConfigSection,
    ScoringConfigSection,
    PipelineLimitsSection,
    HiringManagerSection,
)
from app.models.pipeline import PipelineStage
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse, StageStat
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse, TimelineEntry


class CampaignService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        jd_repo: JDRepository,
        audit_service: AuditService,
        db: Session,
    ):
        self.campaign_repo = campaign_repo
        self.jd_repo = jd_repo
        self.audit_service = audit_service
        self.db = db

    def  create_campaign(
        self,
        request: CampaignCreateRequest,
        org_id: UUID,
        created_by: str
    ) -> CampaignResponse:
        try:
            
            total_weight = request.weight_deterministic + request.weight_semantic + request.weight_ai
            if total_weight != Decimal("100.00"):
                raise CampaignException("Scoring weights must sum to 100.00", 422)

            jd = self.jd_repo.get_by_id(request.jd_id)
            if not jd:
                raise CampaignException(
                    "Invalid job description: Job description not found",
                    422
                )

            if not jd.is_active_version:
                raise CampaignException(
                    "Invalid job description: Job description is not the active version",
                    422
                )

            if jd.closed_at is not None:
                raise CampaignException(
                    "Invalid job description: Job description is closed",
                    422
                )


            existing_campaign = self.campaign_repo.get_by_name(org_id, request.name)
            if existing_campaign:
                raise CampaignException(
                    f"Campaign name '{existing_campaign.name}' already exists in this organization",
                    409)


            if request.deadline:
                if request.deadline <= datetime.now(timezone.utc):
                    raise CampaignException("Campaign deadline must be a future date", 422)

            campaign = HiringCampaign(
                org_id=org_id,
                jd_id=request.jd_id,
                name=request.name.strip(),
                status=CampaignStatus.ACTIVE,
                weight_deterministic=float(request.weight_deterministic),
                weight_semantic=float(request.weight_semantic),
                weight_ai=float(request.weight_ai),
                semantic_threshold=float(request.semantic_threshold),
                ai_threshold=float(request.ai_threshold),
                max_candidates=request.max_candidates,
                deadline=request.deadline,
                hiring_manager_id=request.hiring_manager_id,
                recruiter_id=request.recruiter_id,
                created_by=created_by,
            )

            
            campaign = self.campaign_repo.create_campaign(campaign)

            # Same transaction as the campaign itself: rolled back together on
            # failure. campaign_id is what the activity timeline filters on.
            self.audit_service.log(
                actor_id=created_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_CREATED,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' created",
                    "jd_id": str(campaign.jd_id),
                },
            )

            self.campaign_repo.commit()

            
            hiring_manager_name = None
            if campaign.hiring_manager_id:
                hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
                if hiring_manager:
                    hiring_manager_name = hiring_manager.full_name

            return CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title,
                jd_version=jd.version_number,
                hiring_manager=hiring_manager_name,
                max_candidates=campaign.max_candidates,
                deadline=campaign.deadline,
                created_at=campaign.created_at,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    def get_campaign_by_id(self, campaign_id: UUID) -> CampaignResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(
                f"Campaign with ID '{campaign_id}' not found",
                404,
                None
            )

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        if not jd:
            raise CampaignException(
                "Associated job description not found",
                404,
                None
            )

        hiring_manager_name = None
        if campaign.hiring_manager_id:
            hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
            if hiring_manager:
                hiring_manager_name = hiring_manager.full_name

        return CampaignResponse(
            id=campaign.id,
            name=campaign.name,
            status=campaign.status.value,
            jd_title=jd.title,
            jd_version=jd.version_number,
            hiring_manager=hiring_manager_name,
            max_candidates=campaign.max_candidates,
            deadline=campaign.deadline,
            created_at=campaign.created_at,
        )

    def get_all_campaigns(self) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns()
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,   # ← matches the actual column name
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]
    
    def get_all_campaigns_for_hrAdmin(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hrAdmin(manager_id)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]
    
    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hiring_manager(manager_id)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]

    def get_campaign_details(self,campaign_id: UUID, user:TokenUser) -> CampaignDetailResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found",404)

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        if not jd:
            raise CampaignException("Associated job description not found", 404)

        creator = self.campaign_repo.get_user(campaign.created_by)
        manager = (
            self.campaign_repo.get_user(campaign.hiring_manager_id)
            if campaign.hiring_manager_id
            else None
        )

        is_hiring_manager_only = (
            UserRole.HIRING_MANAGER.value in user.roles
            and UserRole.HR_ADMIN.value not in user.roles
            and UserRole.RECRUITER.value not in user.roles
        )

        return CampaignDetailResponse(
            id=campaign.id,
            campaign_info=CampaignInfoSection(
                name=campaign.name,
                status=campaign.status.value,
                created_by_name=creator.full_name if creator else None,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
            ),
            jd_configuration=JDConfigSection(
                jd_id=jd.id,
                jd_title=jd.title,
                version_number=jd.version_number,
                jurisdiction=jd.jurisdiction,
                mandatory_skill_count=self.campaign_repo.get_mandatory_skill_count(jd.id),
            ),
            # role gate: spec says HM must NOT see weights or manager contact
            scoring_configuration=None if is_hiring_manager_only else ScoringConfigSection(
                weight_deterministic=campaign.weight_deterministic,
                weight_semantic=campaign.weight_semantic,
                weight_ai=campaign.weight_ai,
                semantic_threshold=campaign.semantic_threshold,
                ai_threshold=campaign.ai_threshold,
            ),
            pipeline_limits=PipelineLimitsSection(
                max_candidates=campaign.max_candidates,
                current_candidate_count=self.campaign_repo.get_candidate_count(campaign.id),
                deadline=campaign.deadline,
            ),
            hiring_manager=(None if is_hiring_manager_only else (HiringManagerSection(
                full_name=manager.full_name,
                email=manager.email,
            ) if manager else None)),
        )

    # The true sequential funnel: a candidate normally passes through these in
    # order, so "drop-off between each stage" is meaningful here.
    _FUNNEL_STAGES = (
        PipelineStage.UPLOADED,
        PipelineStage.SCREENING,
        PipelineStage.SHORTLISTED,
        PipelineStage.HM_REVIEW,
        PipelineStage.INTERVIEW,
        PipelineStage.SELECTED,
    )
    # Side buckets a candidate can land in from any funnel stage, not a "next
    # step" after the one before it — comparing counts across these produces
    # meaningless percentages (e.g. REJECTED vs SELECTED), so they're reported
    # with a count only, no drop_off_pct.
    _BUCKET_STAGES = (
        PipelineStage.HOLD,
        PipelineStage.REJECTED,
        PipelineStage.FRAUD_REVIEW,
    )

    def get_pipeline_summary(self, campaign_id: UUID) -> PipelineSummaryResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        counts = self.campaign_repo.get_stage_counts(campaign_id)

        stages: list[StageStat] = []
        prev_count = None

        for stage in self._FUNNEL_STAGES:
            count = counts.get(stage.value, 0)

            drop_off = None
            if prev_count is not None and prev_count > 0:
                drop_off = round((prev_count - count) / prev_count * 100, 1)

            stages.append(StageStat(stage=stage.value, count=count, drop_off_pct=drop_off))
            prev_count = count

        for stage in self._BUCKET_STAGES:
            stages.append(StageStat(stage=stage.value, count=counts.get(stage.value, 0), drop_off_pct=None))

        return PipelineSummaryResponse(
            campaign_id=campaign_id,
            total_candidates=sum(counts.values()),
            stages=stages,
        )

    def get_campaign_timeline(
        self,
        campaign_id: UUID,
        limit: int = 20,
        offset: int = 0,
        event_type: str | None = None,
    ) -> CampaignTimelineResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        entries: list[TimelineEntry] = []

        for log in self.campaign_repo.get_audit_entries(campaign_id):
            detail = log.detail or {}
            entries.append(TimelineEntry(
                timestamp=log.created_at,
                event_type=log.action_type.value,
                actor_name=self._resolve_actor(log.actor_id),
                description=detail.get("title") or log.action_type.value.replace("_", " ").title(),
            ))

        for h in self.campaign_repo.get_stage_history(campaign_id):
            from_stage = h.from_stage.value if h.from_stage else "START"
            entries.append(TimelineEntry(
                timestamp=h.changed_at,
                event_type=f"CANDIDATE_{h.to_stage.value}",
                actor_name=self._resolve_actor(h.changed_by),
                description=f"Candidate moved {from_stage} → {h.to_stage.value}",
            ))

        for job in self.campaign_repo.get_bulk_upload_events(campaign_id):
            if job.status.value == "COMPLETED":
                job_event = "BULK_UPLOAD_COMPLETED"
                summary = f"Bulk upload '{job.original_filename}' completed — {job.processed_count}/{job.total_files} processed"
            elif job.status.value in ("FAILED", "PARTIAL_FAILURE"):
                job_event = "BULK_UPLOAD_FAILED"
                summary = f"Bulk upload '{job.original_filename}' failed — {job.failed_count}/{job.total_files} failed"
            else:
                job_event = "BULK_UPLOAD_STARTED"
                summary = f"Bulk upload '{job.original_filename}' started — {job.total_files} files"

            entries.append(TimelineEntry(
                timestamp=job.completed_at or job.created_at,
                event_type=job_event,
                actor_name=self._resolve_actor(job.uploaded_by),
                description=summary,
            ))

        if event_type:
            entries = [e for e in entries if e.event_type == event_type]

        entries.sort(key=lambda e: e.timestamp, reverse=True)

        return CampaignTimelineResponse(
            campaign_id=campaign_id,
            total_events=len(entries),
            limit=limit,
            offset=offset,
            events=entries[offset: offset + limit],
        )

    def _resolve_actor(self, actor_id: str | None) -> str:
        if not actor_id:
            return "System"
        user = self.campaign_repo.get_user(actor_id)
        return user.full_name if user else "System"

    _SCORING_FIELDS = (
        "weight_deterministic",
        "weight_semantic",
        "weight_ai",
        "semantic_threshold",
        "ai_threshold",
    )

    def update_campaign(
        self,
        campaign_id: UUID,
        request: CampaignUpdateRequest,
        updated_by: str,
    ) -> CampaignResponse:
        try:
            campaign = self.campaign_repo.get_by_id(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

            # ── S07-T03: closed campaigns are read-only ──────────────────
            if campaign.status == CampaignStatus.CLOSED:
                # Log the blocked attempt itself (spec requirement), then commit
                # immediately — the raise below triggers this method's own
                # rollback, which would otherwise erase this audit row too.
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role="HR_ADMIN",
                    action_type=ActionType.CAMPAIGN_EDIT_BLOCKED,
                    entity_type=EntityType.CAMPAIGN,
                    entity_id=campaign.id,
                    campaign_id=campaign.id,
                    details={
                        "title": f"Blocked edit attempt on closed campaign '{campaign.name}'",
                        "attempted_changes": request.model_dump(exclude_unset=True),
                    },
                )
                self.campaign_repo.commit()

                raise CampaignException(
                    "Closed campaigns cannot be edited. Reopen the campaign to make changes.",
                    403,
                )

            changes: dict[str, dict] = {}  # field -> {"before": ..., "after": ...}

            # name / deadline / candidate cap ─────────────────
            if request.name is not None and request.name != campaign.name:
                duplicate = self.campaign_repo.get_by_name(campaign.org_id, request.name)
                if duplicate and duplicate.id != campaign.id:
                    raise CampaignException(
                        f"Campaign name '{request.name}' already exists in this organization",
                        409,
                    )
                changes["name"] = {"before": campaign.name, "after": request.name}
                campaign.name = request.name

            if request.clear_max_candidates:
                if campaign.max_candidates is not None:
                    changes["max_candidates"] = {"before": campaign.max_candidates, "after": None}
                    campaign.max_candidates = None
            elif request.max_candidates is not None and request.max_candidates != campaign.max_candidates:
                current_count = self.campaign_repo.get_candidate_count(campaign.id)
                if request.max_candidates < current_count:
                    raise CampaignException(
                        f"Cannot set candidate cap to {request.max_candidates}: the campaign "
                        f"already has {current_count} candidates.",
                        422,
                    )
                changes["max_candidates"] = {
                    "before": campaign.max_candidates,
                    "after": request.max_candidates,
                }
                campaign.max_candidates = request.max_candidates

            if request.clear_deadline:
                if campaign.deadline is not None:
                    changes["deadline"] = {"before": str(campaign.deadline), "after": None}
                    campaign.deadline = None
            elif request.deadline is not None and request.deadline != campaign.deadline:
                if request.deadline <= datetime.now(timezone.utc):
                    raise CampaignException("Campaign deadline must be a future date", 422)
                changes["deadline"] = {
                    "before": str(campaign.deadline) if campaign.deadline else None,
                    "after": str(request.deadline),
                }
                campaign.deadline = request.deadline

            # ── S07-T02: scoring config gate on ACTIVE campaigns ─────────
            scoring_changes = {
                field: getattr(request, field)
                for field in self._SCORING_FIELDS
                if getattr(request, field) is not None
                and Decimal(str(getattr(campaign, field))) != getattr(request, field)
            }

            if scoring_changes:
                if campaign.status == CampaignStatus.ACTIVE and not request.confirm_scoring_change:
                    raise CampaignException(
                        "Changing scoring configuration will only affect candidates submitted "
                        "after this change. Existing candidate scores will not be recalculated. "
                        "Re-submit with confirm_scoring_change=true to proceed.",
                        422,
                    )

                merged_weights = {
                    field: scoring_changes.get(field, Decimal(str(getattr(campaign, field))))
                    for field in ("weight_deterministic", "weight_semantic", "weight_ai")
                }
                if sum(merged_weights.values()) != Decimal("100.00"):
                    raise CampaignException("Scoring weights must sum to 100.00", 422)

                for field, new_value in scoring_changes.items():
                    changes[field] = {
                        "before": str(getattr(campaign, field)),
                        "after": str(new_value),
                    }
                    setattr(campaign, field, float(new_value))

            if not changes:
                raise CampaignException("No changes supplied", 422)

            campaign.updated_at = datetime.now(timezone.utc)
            campaign = self.campaign_repo.update(campaign)

            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=(
                    ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED
                    if scoring_changes
                    else ActionType.CAMPAIGN_UPDATED
                ),
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' updated",
                    "changes": changes,
                },
            )

            self.campaign_repo.commit()

            jd = self.jd_repo.get_by_id(campaign.jd_id)
            return CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title if jd else "",
                jd_version=jd.version_number if jd else 0,
                hiring_manager=self._resolve_actor(campaign.hiring_manager_id),
                max_candidates=campaign.max_candidates,
                deadline=campaign.deadline,
                created_at=campaign.created_at,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise