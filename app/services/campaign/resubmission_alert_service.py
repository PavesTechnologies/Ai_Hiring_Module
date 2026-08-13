from app.enums.constants import ActionType, EntityType
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.services.audit_service import AuditService

_DEFAULT_THRESHOLD = 3
_DEFAULT_WINDOW_DAYS = 30


class ResubmissionAlertService:
    """
    Epic 3 (M05-E03) Phase C4 — daily sweep flagging candidates submitted to
    an unusually high number of campaigns in a short window. Detection +
    audit logging only: no email/alerting module exists in this codebase
    (the same gap already present in CampaignSchedulerService's health-alert
    feature, which this mirrors).

    Epic 3 Fix 2 (2026-08-11): previously attributed actor_id to the
    candidate's most recent campaign's created_by, on the premise that
    AuditLog.actor_id was a required, non-null FK with no synthetic SYSTEM
    actor available. That premise was false - actor_id is nullable in both
    the model and the live schema, and StageTransitionService.transition()
    already logs SYSTEM-triggered writes with actor_id=None/actor_role=
    SYSTEM correctly. Fixed to do the same here - this is a scheduled
    sweep, not an action the campaign's creator took.
    """

    def __init__(
        self,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        config_repo: ConfigRepository,
    ):
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        self.config_repo = config_repo

    def evaluate_resubmission_alerts(self) -> int:
        """Returns the number of alerts raised."""
        thresholds = self.config_repo.get_configs_by_keys([
            "CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD",
            "CROSS_CAMPAIGN_SUBMISSION_WINDOW_DAYS",
        ])
        threshold = int(thresholds.get("CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD", _DEFAULT_THRESHOLD))
        window_days = int(thresholds.get("CROSS_CAMPAIGN_SUBMISSION_WINDOW_DAYS", _DEFAULT_WINDOW_DAYS))

        flagged = self.campaign_candidate_repo.get_high_frequency_resubmissions(window_days, threshold)

        alerts_raised = 0
        for candidate_id, submission_count in flagged:
            campaign = self.campaign_candidate_repo.get_most_recent_campaign_for_candidate(candidate_id)
            if campaign is None:
                continue

            self.audit_service.log(
                actor_id=None,
                actor_role="SYSTEM",
                action_type=ActionType.CAMPAIGN_RESUBMISSION_DETECTED,
                entity_type=EntityType.CANDIDATE,
                entity_id=candidate_id,
                campaign_id=campaign.id,
                details={
                    "submission_count": submission_count,
                    "window_days": window_days,
                    "threshold": threshold,
                },
            )
            alerts_raised += 1

        self.campaign_candidate_repo.commit()
        return alerts_raised
