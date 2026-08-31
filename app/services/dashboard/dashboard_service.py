import json
import logging
from datetime import datetime, timedelta, timezone

from app.core.cache_keys import dashboard_key
from app.core.config import settings
from app.core.encryption_service import DecryptionError, EncryptionService
from app.enums.constants import ActionType
from app.models.campaigns import CampaignStatus
from app.models.candidates import Candidate, Resume
from app.models.pipeline import PipelineStage
from app.repositories.config_repository import ConfigRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dashboard.dashboard_response import (
    CircuitBreakerHealthResponse,
    DashboardCampaignCardResponse,
    DashboardStatsResponse,
    FunnelStageCount,
    HiringFunnelResponse,
    HrAdminDashboardSummaryResponse,
    NavBadgeCountsResponse,
    NotificationItemResponse,
    NotificationsFeedResponse,
    RecruiterDashboardSummaryResponse,
    StageTimingResponse,
    StatTileResponse,
    TopCandidateResponse,
    TopCandidatesResponse,
)
from app.services.cache_service import CacheService

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

    def __init__(
        self,
        dashboard_repo: DashboardRepository,
        encryption_service: EncryptionService,
        config_repo: ConfigRepository,
        cache_service: CacheService | None = None,
    ):
        self.dashboard_repo = dashboard_repo
        self.encryption_service = encryption_service
        self.config_repo = config_repo
        self.cache_service = cache_service

    # ── stat tiles / hiring funnel / top candidates / notifications ────

    def get_stats(self) -> DashboardStatsResponse:
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        quarter_start, prev_quarter_start = self._quarter_bounds(now)

        open_campaigns_total = self.dashboard_repo.count_campaigns(status=CampaignStatus.ACTIVE)
        open_campaigns_new_this_week = self.dashboard_repo.count_campaigns(
            status=CampaignStatus.ACTIVE, created_after=week_ago,
        )

        candidates_in_pipeline_total = self.dashboard_repo.count_candidates_in_pipeline()
        candidates_added_this_week = self.dashboard_repo.count_candidates_in_pipeline(created_after=week_ago)

        avg_days_current = self.dashboard_repo.avg_time_to_hire_days(since=quarter_start)
        avg_days_previous = self.dashboard_repo.avg_time_to_hire_days(since=prev_quarter_start, until=quarter_start)

        offers_current = self.dashboard_repo.count_selected(since=quarter_start)
        offers_previous = self.dashboard_repo.count_selected(since=prev_quarter_start, until=quarter_start)

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

        uploaded_count = self.dashboard_repo.count_uploaded_resumes(created_after=since)
        parsing_count = self.dashboard_repo.count_parsing_resumes(created_after=since)
        stage_counts = self.dashboard_repo.get_pipeline_stage_counts(
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
        rows = self.dashboard_repo.get_top_candidates(limit, campaign_status=CampaignStatus.ACTIVE)

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
        entries = self.dashboard_repo.get_recent_audit_entries(
            limit, action_types=list(_ACTION_MESSAGE_TEMPLATES.keys()),
        )

        actor_ids = {str(e.actor_id) for e in entries if e.actor_id}
        actor_names = self.dashboard_repo.get_user_names(list(actor_ids))

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

    # ── HR_ADMIN / RECRUITER summaries, campaign cards, nav badges, stage timing ──

    def _sla_thresholds(self) -> tuple[float, int, int]:
        """
        Same keys and same fallbacks CampaignService._get_review_stall_thresholds
        uses, so a stall counted on the dashboard is a stall shown on the
        campaign's Stalled tab.
        """
        configs = self.config_repo.get_configs_by_keys(
            ["SCREENING_SLA_HOURS", "HM_REVIEW_SLA_DAYS", "INTERVIEW_SLA_DAYS"]
        )
        return (
            float(configs.get("SCREENING_SLA_HOURS", "48")),
            int(configs.get("HM_REVIEW_SLA_DAYS", "5")),
            int(configs.get("INTERVIEW_SLA_DAYS", "7")),
        )

    def _warning_thresholds(self) -> tuple[float, int]:
        configs = self.config_repo.get_configs_by_keys(
            ["CAP_WARNING_PERCENTAGE", "DEADLINE_WARNING_DAYS"]
        )
        return (
            float(configs.get("CAP_WARNING_PERCENTAGE", "80.00")),
            int(configs.get("DEADLINE_WARNING_DAYS", "3")),
        )

    def get_hr_admin_summary(self, user_id: str) -> HrAdminDashboardSummaryResponse:
        if not self.cache_service:
            return self._load_hr_admin_summary(user_id)
        raw = self.cache_service.get_or_set(
            dashboard_key("hr-admin", {"user_id": user_id}),
            loader=lambda: self._load_hr_admin_summary(user_id).model_dump_json(),
            ttl=settings.cache_dashboard_ttl_seconds,
        )
        return HrAdminDashboardSummaryResponse.model_validate_json(raw)

    def _load_hr_admin_summary(self, user_id: str) -> HrAdminDashboardSummaryResponse:
        screening, hm_review, interview = self._sla_thresholds()
        metrics = self.dashboard_repo.get_hr_admin_metrics(
            screening_sla_hours=screening,
            hm_review_sla_days=hm_review,
            interview_sla_days=interview,
        )

        return HrAdminDashboardSummaryResponse(
            **metrics,
            platform_health=[
                CircuitBreakerHealthResponse(
                    service_name=b.service_name,
                    state=b.state.value,
                    failure_count=b.failure_count,
                    opened_at=b.opened_at,
                    retry_after=b.retry_after,
                )
                for b in self.dashboard_repo.get_circuit_breaker_states()
            ],
            last_login_at=self.dashboard_repo.get_last_login_at(user_id),
            generated_at=datetime.now(timezone.utc),
        )

    def get_recruiter_summary(self, user_id: str) -> RecruiterDashboardSummaryResponse:
        if not self.cache_service:
            return self._load_recruiter_summary(user_id)
        raw = self.cache_service.get_or_set(
            dashboard_key("recruiter", {"user_id": user_id}),
            loader=lambda: self._load_recruiter_summary(user_id).model_dump_json(),
            ttl=settings.cache_dashboard_ttl_seconds,
        )
        return RecruiterDashboardSummaryResponse.model_validate_json(raw)

    def _load_recruiter_summary(self, user_id: str) -> RecruiterDashboardSummaryResponse:
        return RecruiterDashboardSummaryResponse(
            **self.dashboard_repo.get_recruiter_metrics(user_id),
            last_login_at=self.dashboard_repo.get_last_login_at(user_id),
            generated_at=datetime.now(timezone.utc),
        )

    def get_campaign_cards(
        self,
        *,
        recruiter_id: str | None,
        show_closed: bool = False,
        limit: int = 12,
        search: str | None = None,
        status: CampaignStatus | None = None,
    ) -> list[DashboardCampaignCardResponse]:
        params = {
            "recruiter_id": recruiter_id, "show_closed": show_closed, "limit": limit,
            "search": search, "status": status.value if status else None,
        }
        if not self.cache_service:
            return self._load_campaign_cards(**params)
        raw = self.cache_service.get_or_set(
            dashboard_key("campaign-cards", params),
            loader=lambda: json.dumps(
                [card.model_dump(mode="json") for card in self._load_campaign_cards(**params)]
            ),
            ttl=settings.cache_dashboard_ttl_seconds,
        )
        return [DashboardCampaignCardResponse.model_validate(item) for item in json.loads(raw)]

    def _load_campaign_cards(
        self,
        *,
        recruiter_id: str | None,
        show_closed: bool = False,
        limit: int = 12,
        search: str | None = None,
        status: str | None = None,
    ) -> list[DashboardCampaignCardResponse]:
        status = CampaignStatus(status) if status else None
        screening, hm_review, interview = self._sla_thresholds()
        cap_warning_percentage, deadline_warning_days = self._warning_thresholds()

        rows = self.dashboard_repo.get_campaign_cards(
            recruiter_id=recruiter_id,
            show_closed=show_closed,
            limit=limit,
            screening_sla_hours=screening,
            hm_review_sla_days=hm_review,
            interview_sla_days=interview,
            search=search,
            status=status,
        )

        # one batched lookup instead of a name query per card
        names = self.dashboard_repo.get_user_names(
            [r.hiring_manager_id for r in rows if r.hiring_manager_id]
        )
        now = datetime.now(timezone.utc)

        cards: list[DashboardCampaignCardResponse] = []
        for r in rows:
            # max_candidates counts openings, so "approaching cap" is measured
            # against SELECTED candidates - never total intake.
            approaching_cap = bool(
                r.max_candidates
                and r.selected_count >= (r.max_candidates * (cap_warning_percentage / 100))
            )
            days_left = (r.deadline - now).days if r.deadline else None

            cards.append(
                DashboardCampaignCardResponse(
                    id=r.id,
                    name=r.name,
                    status=r.status.value if hasattr(r.status, "value") else str(r.status),
                    jd_id=r.jd_id,
                    jd_title=r.jd_title,
                    jd_version=r.jd_version,
                    hiring_manager=names.get(str(r.hiring_manager_id)) if r.hiring_manager_id else None,
                    max_candidates=r.max_candidates,
                    deadline=r.deadline,
                    created_at=r.created_at,
                    candidate_count=r.candidate_count or 0,
                    shortlisted_count=r.shortlisted_count or 0,
                    selected_count=r.selected_count or 0,
                    hm_review_count=r.hm_review_count or 0,
                    ai_failure_count=r.ai_failure_count or 0,
                    stalled_count=r.stalled_count or 0,
                    approaching_cap=approaching_cap,
                    deadline_soon=bool(days_left is not None and 0 <= days_left <= deadline_warning_days),
                    is_overdue=bool(days_left is not None and days_left < 0),
                )
            )
        return cards

    def get_nav_badges(self, recruiter_id: str | None) -> NavBadgeCountsResponse:
        if not self.cache_service:
            return self._load_nav_badges(recruiter_id)
        raw = self.cache_service.get_or_set(
            dashboard_key("nav-badges", {"recruiter_id": recruiter_id}),
            loader=lambda: self._load_nav_badges(recruiter_id).model_dump_json(),
            ttl=settings.cache_dashboard_badge_ttl_seconds,
        )
        return NavBadgeCountsResponse.model_validate_json(raw)

    def _load_nav_badges(self, recruiter_id: str | None) -> NavBadgeCountsResponse:
        return NavBadgeCountsResponse(
            **self.dashboard_repo.get_nav_badge_counts(recruiter_id),
            generated_at=datetime.now(timezone.utc),
        )

    def get_stage_timing(self, campaign_id) -> list[StageTimingResponse]:
        if not self.cache_service:
            return self._load_stage_timing(campaign_id)
        raw = self.cache_service.get_or_set(
            dashboard_key("stage-timing", {"campaign_id": str(campaign_id)}),
            loader=lambda: json.dumps(
                [row.model_dump(mode="json") for row in self._load_stage_timing(campaign_id)]
            ),
            ttl=settings.cache_dashboard_stage_timing_ttl_seconds,
        )
        return [StageTimingResponse.model_validate(item) for item in json.loads(raw)]

    def _load_stage_timing(self, campaign_id) -> list[StageTimingResponse]:
        """
        Per-stage dwell times with the configured SLA attached, so the UI can
        draw the reference line and flag breaches without re-reading config.
        """
        screening, hm_review, interview = self._sla_thresholds()
        sla_by_stage = {
            PipelineStage.SCREENING.value: screening / 24.0,   # config is in hours
            PipelineStage.HM_REVIEW.value: float(hm_review),
            PipelineStage.INTERVIEW.value: float(interview),
        }

        out: list[StageTimingResponse] = []
        for row in self.dashboard_repo.get_stage_timing(campaign_id):
            stage = row.stage.value if hasattr(row.stage, "value") else str(row.stage)
            avg_days = float(row.avg_days or 0)
            max_days = float(row.max_days or 0)
            sla = sla_by_stage.get(stage)
            out.append(
                StageTimingResponse(
                    stage=stage,
                    candidate_count=row.candidate_count or 0,
                    avg_days=round(avg_days, 2),
                    max_days=round(max_days, 2),
                    sla_days=round(sla, 2) if sla is not None else None,
                    breaches_sla=bool(sla is not None and max_days > sla),
                )
            )
        # stable, funnel-ish ordering rather than whatever GROUP BY returned
        order = [s.value for s in PipelineStage]
        out.sort(key=lambda r: order.index(r.stage) if r.stage in order else 99)
        return out
