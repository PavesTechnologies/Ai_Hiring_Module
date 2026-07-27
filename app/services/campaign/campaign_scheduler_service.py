from datetime import datetime, timezone

from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.config_repository import ConfigRepository
from app.services.audit_service import AuditService


class CampaignSchedulerService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
        config_repo: ConfigRepository,
    ):
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        self.config_repo = config_repo

    def auto_close_expired_campaigns(self, batch_size: int = 100) -> int:
        """
        Auto close campaigns whose deadline has expired.

        E03-S05-T02: processed in batches (each batch committed separately)
        rather than one giant transaction locking every expired row at once
        — the ACTIVE-status filter in get_expired_campaigns() naturally
        excludes campaigns already closed by a prior batch, so no offset
        bookkeeping is needed for the loop to converge.

        Returns:
            int: Number of campaigns closed.
        """

        total_closed = 0

        try:
            while True:
                batch = self.campaign_repo.get_expired_campaigns(limit=batch_size)
                if not batch:
                    break

                for campaign in batch:

                    self.campaign_repo.close_campaign(campaign)

                    # Audit log — attributed to the HR_ADMIN who created the campaign,
                    # since the closure is triggered by the scheduler on their behalf.
                    self.audit_service.log(
                        actor_id=campaign.created_by,
                        actor_role="HR_ADMIN",
                        action_type=ActionType.CAMPAIGN_AUTO_CLOSED.value,
                        entity_type=EntityType.CAMPAIGN.value,
                        entity_id=campaign.id,
                        campaign_id=campaign.id,
                        details={
                            "title": f"Campaign '{campaign.name}' auto-closed",
                            "reason": "DEADLINE_EXPIRED",
                            "deadline": campaign.deadline.isoformat()
                            if campaign.deadline
                            else None,
                            "closed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                    # Email notification
                    # TODO:
                    # Notify HR Admin
                    # Notify Hiring Manager

                    total_closed += 1

                self.campaign_repo.commit()

                if len(batch) < batch_size:
                    break

            return total_closed

        except Exception:
            self.campaign_repo.rollback()
            raise

    def evaluate_campaign_health_alerts(self) -> int:
        """
        E04-S01-T03: daily health check across every ACTIVE campaign,
        evaluating 4 independent conditions against platform_config
        thresholds. Each triggered condition gets its own
        CAMPAIGN_HEALTH_ALERT audit entry — a campaign can trigger more
        than one alert in the same run.

        Returns:
            int: total number of alerts raised across all campaigns.
        """
        thresholds = self.config_repo.get_configs_by_keys([
            "DEAD_TASK_ALERT_THRESHOLD",
            "DETERMINISTIC_REJECTION_ALERT_THRESHOLD",
            "SCREENING_SLA_HOURS",
            "FRAUD_ALERT_THRESHOLD",
        ])
        dead_task_threshold = int(thresholds.get("DEAD_TASK_ALERT_THRESHOLD", "5"))
        rejection_rate_threshold = float(thresholds.get("DETERMINISTIC_REJECTION_ALERT_THRESHOLD", "80.00"))
        screening_sla_hours = float(thresholds.get("SCREENING_SLA_HOURS", "48"))
        fraud_threshold = int(thresholds.get("FRAUD_ALERT_THRESHOLD", "3"))

        alerts_raised = 0

        try:
            active_campaigns = [
                c for c in self.campaign_repo.get_all_campaigns(show_closed=False)
                if c.status == CampaignStatus.ACTIVE
            ]

            for campaign in active_campaigns:
                task_counts = self.campaign_repo.get_task_status_counts(campaign.id)
                dead_count = task_counts.get(TaskStatus.DEAD.value, 0)
                if dead_count > dead_task_threshold:
                    self._raise_health_alert(
                        campaign,
                        condition="DEAD_TASK_COUNT_EXCEEDED",
                        metric_detail={"dead_task_count": dead_count, "threshold": dead_task_threshold},
                    )
                    alerts_raised += 1

                rejection_rate = self.campaign_repo.get_deterministic_rejection_rate(campaign.id)
                if rejection_rate > rejection_rate_threshold:
                    self._raise_health_alert(
                        campaign,
                        condition="DETERMINISTIC_REJECTION_RATE_EXCEEDED",
                        metric_detail={
                            "rejection_rate_pct": round(rejection_rate, 2),
                            "threshold_pct": rejection_rate_threshold,
                        },
                    )
                    alerts_raised += 1

                avg_screening_hours = self.campaign_repo.get_average_screening_hours(campaign.id)
                if avg_screening_hours is not None and avg_screening_hours > screening_sla_hours:
                    self._raise_health_alert(
                        campaign,
                        condition="SCREENING_SLA_EXCEEDED",
                        metric_detail={
                            "average_screening_hours": round(avg_screening_hours, 2),
                            "threshold_hours": screening_sla_hours,
                        },
                    )
                    alerts_raised += 1

                stage_counts = self.campaign_repo.get_stage_counts(campaign.id)
                fraud_count = stage_counts.get(PipelineStage.FRAUD_REVIEW.value, 0)
                if fraud_count > fraud_threshold:
                    self._raise_health_alert(
                        campaign,
                        condition="FRAUD_REVIEW_COUNT_EXCEEDED",
                        metric_detail={"fraud_review_count": fraud_count, "threshold": fraud_threshold},
                    )
                    alerts_raised += 1

            self.campaign_repo.commit()
            return alerts_raised

        except Exception:
            self.campaign_repo.rollback()
            raise

    def _raise_health_alert(self, campaign, condition: str, metric_detail: dict) -> None:
        # Email notification
        # TODO:
        # Notify HR Admin
        self.audit_service.log(
            actor_id=campaign.created_by,
            actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_HEALTH_ALERT.value,
            entity_type=EntityType.CAMPAIGN.value,
            entity_id=campaign.id,
            campaign_id=campaign.id,
            details={
                "title": f"Health alert for campaign '{campaign.name}'",
                "condition": condition,
                **metric_detail,
            },
        )