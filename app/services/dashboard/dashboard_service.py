from datetime import datetime, timezone

from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage
from app.repositories.config_repository import ConfigRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dashboard.dashboard_response import (
    CircuitBreakerHealthResponse,
    DashboardCampaignCardResponse,
    HrAdminDashboardSummaryResponse,
    NavBadgeCountsResponse,
    RecruiterDashboardSummaryResponse,
    StageTimingResponse,
)


class DashboardService:

    def __init__(self, dashboard_repo: DashboardRepository, config_repo: ConfigRepository):
        self.dashboard_repo = dashboard_repo
        self.config_repo = config_repo

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
        hiring_manager_id: str | None = None,
    ) -> list[DashboardCampaignCardResponse]:
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
            hiring_manager_id=hiring_manager_id,
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
        return NavBadgeCountsResponse(
            **self.dashboard_repo.get_nav_badge_counts(recruiter_id),
            generated_at=datetime.now(timezone.utc),
        )

    def get_stage_timing(self, campaign_id) -> list[StageTimingResponse]:
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
