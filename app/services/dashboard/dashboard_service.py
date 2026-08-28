import logging
from datetime import datetime, timedelta, timezone

from app.core.encryption_service import DecryptionError, EncryptionService
from app.enums.constants import ActionType
from app.models.campaigns import CampaignStatus
from app.models.candidates import Candidate, Resume
from app.models.pipeline import PipelineStage
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dashboard.dashboard_response import (
    DashboardStatsResponse,
    FunnelStageCount,
    HiringFunnelResponse,
    NotificationItemResponse,
    NotificationsFeedResponse,
    StatTileResponse,
    TopCandidateResponse,
    TopCandidatesResponse,
)

logger = logging.getLogger(__name__)

# Best-effort mapping of audit-log events onto the dashboard's "Tasks &
# notifications" feed — there is no dedicated notifications table yet, so
# this reuses the existing compliance audit trail (app/models/compliance.py)
# rather than inventing a parallel one. Events with no template are excluded
# rather than shown with a raw ActionType-derived string.
_ACTION_MESSAGE_TEMPLATES: dict[ActionType, str] = {
    ActionType.CAMPAIGN_CREATED: "Campaign created",
    ActionType.CAMPAIGN_CLOSED: "Campaign closed",
    ActionType.CAMPAIGN_PAUSED: "Campaign paused",
    ActionType.CAMPAIGN_RESUMED: "Campaign resumed",
    ActionType.CAMPAIGN_REOPENED: "Campaign reopened",
    ActionType.CAMPAIGN_HEALTH_ALERT: "Campaign health alert raised",
    ActionType.CANDIDATE_ADDED: "Candidate added to campaign",
    ActionType.CANDIDATE_REMOVED: "Candidate removed from campaign",
    ActionType.RESUME_UPLOADED: "Resume uploaded",
    ActionType.JD_CREATED: "Job description created",
    ActionType.JD_CLOSED: "Job description closed",
}

_FUNNEL_STAGE_LABELS: list[tuple[str, str]] = [
    ("UPLOADED", "Uploaded"),
    ("PARSING", "Parsing"),
    ("SCREENING", "Screening"),
    ("SHORTLISTED", "Shortlisted"),
    ("INTERVIEW", "Interview"),
    ("SELECTED", "Selected"),
]


class DashboardService:

    def __init__(self,
        repo: DashboardRepository,
        encryption_service: EncryptionService,
    ):
        self.repo = repo
        self.encryption_service = encryption_service

    def get_stats(self) -> DashboardStatsResponse:
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        quarter_start, prev_quarter_start = self._quarter_bounds(now)

        open_campaigns_total = self.repo.count_campaigns(status=CampaignStatus.ACTIVE)
        open_campaigns_new_this_week = self.repo.count_campaigns(
            status=CampaignStatus.ACTIVE, created_after=week_ago,
        )

        candidates_in_pipeline_total = self.repo.count_candidates_in_pipeline()
        candidates_added_this_week = self.repo.count_candidates_in_pipeline(created_after=week_ago)

        avg_days_current = self.repo.avg_time_to_hire_days(since=quarter_start)
        avg_days_previous = self.repo.avg_time_to_hire_days(since=prev_quarter_start, until=quarter_start)

        offers_current = self.repo.count_selected(since=quarter_start)
        offers_previous = self.repo.count_selected(since=prev_quarter_start, until=quarter_start)

        return DashboardStatsResponse(
            open_campaigns=StatTileResponse(
                value=open_campaigns_total,
                delta=open_campaigns_new_this_week,
                delta_label="this week",
            ),
            candidates_in_pipeline=StatTileResponse(
                value=candidates_in_pipeline_total,
                delta=candidates_added_this_week,
                delta_label="this week",
            ),
            avg_time_to_hire_days=StatTileResponse(
                value=round(avg_days_current, 1) if avg_days_current is not None else 0.0,
                unit="days",
                delta=(
                    round(avg_days_current - avg_days_previous, 1)
                    if avg_days_current is not None and avg_days_previous is not None
                    else None
                ),
                delta_label="vs previous quarter",
                is_estimate=True,
            ),
            offers_this_quarter=StatTileResponse(
                value=offers_current,
                delta=offers_current - offers_previous,
                delta_label="vs previous quarter",
                is_estimate=True,
            ),
        )

    def get_hiring_funnel(self, days: int | None = 30) -> HiringFunnelResponse:
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

        uploaded_count = self.repo.count_uploaded_resumes(created_after=since)
        parsing_count = self.repo.count_parsing_resumes(created_after=since)
        stage_counts = self.repo.get_pipeline_stage_counts(
            created_after=since, campaign_status=CampaignStatus.ACTIVE,
        )

        counts = {
            "UPLOADED": uploaded_count,
            "PARSING": parsing_count,
            "SCREENING": stage_counts.get(PipelineStage.SCREENING.value, 0),
            "SHORTLISTED": stage_counts.get(PipelineStage.SHORTLISTED.value, 0),
            "INTERVIEW": stage_counts.get(PipelineStage.INTERVIEW.value, 0),
            "SELECTED": stage_counts.get(PipelineStage.SELECTED.value, 0),
        }

        stages = [
            FunnelStageCount(stage=key, label=label, count=counts[key])
            for key, label in _FUNNEL_STAGE_LABELS
        ]

        return HiringFunnelResponse(
            range_days=days,
            total_candidates=uploaded_count,
            stages=stages,
        )

    def get_top_candidates(self, limit: int) -> TopCandidatesResponse:
        rows = self.repo.get_top_candidates(limit, campaign_status=CampaignStatus.ACTIVE)

        candidates = []
        for campaign_candidate, candidate, resume, campaign in rows:
            designation, _ = self._extract_designation_and_experience(resume)
            candidates.append(TopCandidateResponse(
                campaign_candidate_id=campaign_candidate.id,
                candidate_id=candidate.id,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                candidate_name=self._decrypt_candidate_name(candidate),
                current_designation=designation,
                composite_score=float(campaign_candidate.composite_score),
            ))

        return TopCandidatesResponse(candidates=candidates)

    def get_notifications(self, limit: int) -> NotificationsFeedResponse:
        entries = self.repo.get_recent_audit_entries(
            limit, action_types=list(_ACTION_MESSAGE_TEMPLATES.keys()),
        )

        actor_ids = {str(e.actor_id) for e in entries if e.actor_id}
        actor_names = self.repo.get_user_names(list(actor_ids))

        items = []
        for entry in entries:
            detail = entry.detail or {}
            message = detail.get("title") or _ACTION_MESSAGE_TEMPLATES[entry.action_type]
            actor_name = actor_names.get(str(entry.actor_id), "System") if entry.actor_id else "System"

            items.append(NotificationItemResponse(
                id=str(entry.id),
                event_type=entry.action_type.value,
                message=message,
                actor_name=actor_name,
                campaign_id=entry.campaign_id,
                created_at=entry.created_at,
            ))

        return NotificationsFeedResponse(items=items)

    def _decrypt_candidate_name(self, candidate: Candidate | None) -> str | None:
        if candidate is None or not candidate.full_name_encrypted:
            return None
        try:
            return self.encryption_service.decrypt(
                candidate.full_name_encrypted, candidate.encryption_key_id,
            )
        except DecryptionError:
            logger.exception("Failed to decrypt candidate name for candidate_id=%s", candidate.id)
            return None

    @staticmethod
    def _extract_designation_and_experience(
        resume: Resume | None,
    ) -> tuple[str | None, float | None]:
        if resume is None or not resume.parsed_json:
            return None, None

        parsed = resume.parsed_json
        experience = parsed.get("total_experience_years")

        work_experience = parsed.get("work_experience") or []
        current_entry = next((entry for entry in work_experience if entry.get("is_current")), None)
        entry = current_entry or (work_experience[0] if work_experience else None)
        designation = entry.get("title") if entry else None

        return designation, experience

    @staticmethod
    def _quarter_bounds(now: datetime) -> tuple[datetime, datetime]:
        quarter_start_month = ((now.month - 1) // 3) * 3 + 1
        quarter_start = now.replace(
            month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0,
        )

        prev_month = quarter_start_month - 3
        prev_year = quarter_start.year
        if prev_month <= 0:
            prev_month += 12
            prev_year -= 1
        prev_quarter_start = quarter_start.replace(year=prev_year, month=prev_month)

        return quarter_start, prev_quarter_start
