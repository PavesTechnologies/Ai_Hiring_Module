import logging
from dataclasses import dataclass

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate
from app.models.pipeline import CampaignCandidate, PipelineStage

logger = logging.getLogger(__name__)

# Same set deterministic_scoring_tasks._SCOREABLE_CAMPAIGN_STATUSES already
# gates the underlying calculate_deterministic_score_task on - reusing it
# here means a candidate this validator clears is never silently re-skipped
# by the task itself for a campaign-status reason it already checked.
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# Only a candidate still strictly before SHORTLISTED is eligible for
# automatic re-scoring - HR has not yet acted on them, so an unknown-skill
# resolution changing their deterministic inputs is still safe to apply
# without a human back in the loop. PipelineStage (app/models/pipeline.py)
# has no OFFER/HIRED/WITHDRAWN/ARCHIVED members in this schema - HOLD,
# HM_REVIEW, INTERVIEW, SELECTED, REJECTED and FRAUD_REVIEW are all this
# schema's "already acted on" stages, and are all excluded by this same
# "before SHORTLISTED" rule rather than needing their own individual check.
_REEVALUATION_ELIGIBLE_STAGES = {PipelineStage.UPLOADED, PipelineStage.SCREENING}


@dataclass
class ReEvaluationDecision:
    allowed: bool
    skip_reason: str | None = None


class CandidateReEvaluationValidator:
    """
    Business gate in front of re-triggering deterministic scoring for a
    candidate whose candidate_skills changed as a result of an HR
    unknown-skill resolution (map/promote/resolve/bulk-approve — see
    SkillCurationService). Applies only to this after-the-fact
    re-evaluation path, never to the original first-pass scoring a resume
    gets straight out of process_resume_document.
    """

    def evaluate(
        self,
        campaign_candidate: CampaignCandidate,
        candidate: Candidate | None,
        campaign: HiringCampaign | None,
    ) -> ReEvaluationDecision:
        if candidate is None:
            return ReEvaluationDecision(False, "Candidate record not found.")
        if candidate.is_pii_deleted:
            return ReEvaluationDecision(
                False, "Candidate has been erased (is_pii_deleted=True) - not active."
            )

        if campaign is None:
            return ReEvaluationDecision(False, "Hiring campaign not found.")
        if campaign.status not in _SCOREABLE_CAMPAIGN_STATUSES:
            allowed_values = ", ".join(status.value for status in _SCOREABLE_CAMPAIGN_STATUSES)
            return ReEvaluationDecision(
                False,
                f"Campaign status '{campaign.status.value}' does not allow reprocessing "
                f"(only {allowed_values} do).",
            )

        if campaign_candidate.pipeline_stage not in _REEVALUATION_ELIGIBLE_STAGES:
            return ReEvaluationDecision(
                False,
                f"Candidate pipeline_stage is '{campaign_candidate.pipeline_stage.value}' - "
                "automatic re-evaluation only applies to candidates still before SHORTLISTED.",
            )

        return ReEvaluationDecision(True)
