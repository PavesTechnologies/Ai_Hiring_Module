"""
Recruiter overrides (Phase 4): a person moving a candidate against the system's screening outcome is
recorded, with a reason code (never free text), the result it contradicted and who moved them.

Only moves by a person, and only while the screening decision stands - from SCREENING, SHORTLISTED,
HOLD or REJECTED. Later moves (after HM review) are hiring decisions, not overrides.

  system outcome   moved to                                          recorded as
  PASS             REJECTED or HOLD                                  OVERRIDE, DOWN
  REJECT           SCREENING (re-evaluate), HOLD, or forward          OVERRIDE, UP
  HOLD             forward (SHORTLISTED, HM_REVIEW, INTERVIEW, ...)  REVIEW_DECISION, UP
  HOLD             REJECTED                                          REVIEW_DECISION, DOWN

Anything else agrees with the outcome or is neutral (fraud review, reset to UPLOADED, re-screening a
PASS or a HOLD). The system outcome is the candidate's latest scoring result acted on.

Both kinds need a reason code. Without one the move is refused with a 422 whose data names the
codes (OVERRIDE_REASON_REQUIRED), so the screen can ask for one and send the move again.
"""
from app.exceptions.campaign_exceptions import CampaignException
from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.pipeline import PipelineStage
from app.models.scoring_v2 import CandidateScoringResult

REASON_CODES = {
    "SKILL_EVIDENCE_MISSED": "Skill evidence missed",
    "SKILL_EVIDENCE_WRONG": "Skill evidence wrong",
    "EXPERIENCE_JUDGEMENT": "Experience judgement",
    "EDUCATION_EQUIVALENT": "Education equivalent",
    "ROLE_FIT": "Role fit",
    "HIRING_MANAGER_REQUEST": "Hiring manager request",
    "JD_CALIBRATION_WRONG": "JD calibration wrong",
    "OTHER": "Other",
}
KIND_OVERRIDE, KIND_REVIEW_DECISION = "OVERRIDE", "REVIEW_DECISION"
UP, DOWN = "UP", "DOWN"
REQUIRED_CODE = "OVERRIDE_REASON_REQUIRED"
UNKNOWN_CODE = "OVERRIDE_REASON_UNKNOWN"

SCREENING_DECISION_STAGES = {PipelineStage.SCREENING, PipelineStage.SHORTLISTED, PipelineStage.HOLD,
                             PipelineStage.REJECTED}
FORWARD = {PipelineStage.SHORTLISTED, PipelineStage.HM_REVIEW, PipelineStage.INTERVIEW, PipelineStage.SELECTED}


def normal_outcome(outcome: str | None) -> str | None:
    return {"REVIEW": "HOLD"}.get(outcome, outcome)


def classify(system_outcome: str | None, from_stage: PipelineStage, to_stage: PipelineStage) -> tuple[str, str] | None:
    """(kind, direction) when the move goes against the outcome or resolves a HOLD, else None."""
    outcome = normal_outcome(system_outcome)
    if from_stage not in SCREENING_DECISION_STAGES or from_stage == to_stage:
        return None
    if outcome == "PASS" and to_stage in (PipelineStage.REJECTED, PipelineStage.HOLD):
        return KIND_OVERRIDE, DOWN
    if outcome == "REJECT" and (to_stage in FORWARD or to_stage in (PipelineStage.HOLD, PipelineStage.SCREENING)):
        return KIND_OVERRIDE, UP
    if outcome == "HOLD" and to_stage in FORWARD:
        return KIND_REVIEW_DECISION, UP
    if outcome == "HOLD" and to_stage == PipelineStage.REJECTED:
        return KIND_REVIEW_DECISION, DOWN
    return None


def acted_on_result(db, campaign_candidate_id) -> CandidateScoringResult | None:
    """The candidate's latest scoring result acted on - the system's outcome."""
    return (db.query(CandidateScoringResult)
            .filter(CandidateScoringResult.campaign_candidate_id == campaign_candidate_id,
                    CandidateScoringResult.acted_on.is_(True))
            .order_by(CandidateScoringResult.created_at.desc())
            .first())


def reason_code_options() -> list[dict]:
    return [{"code": code, "label": label} for code, label in REASON_CODES.items()]


def _missing_core(breakdown: dict) -> list[str]:
    paraphrased = {str(r.get("canonical_skill_id")) for r in (breakdown.get("paraphrase") or {}).get("results") or []
                   if r.get("status") == "CONFIRMED"}
    v2 = [r.get("name") for r in (breakdown.get("confirmation") or {}).get("results") or []
          if r.get("status") != "CONFIRMED" and str(r.get("canonical_skill_id")) not in paraphrased]
    if v2:
        return sorted(n for n in v2 if n)
    covered = {"EXACT", "CHILD", "GRANDCHILD", "SIBLING", "SEMANTIC"}             # v1
    return sorted(e["canonical_name"] for e in breakdown.get("mandatory_skills") or []
                  if e.get("importance") == "core" and e.get("match_type") not in covered and e.get("canonical_name"))


def _template(breakdown: dict) -> str | None:
    value = breakdown.get("profile_template")
    value = value.get("key") if isinstance(value, dict) else value
    return value if isinstance(value, str) else None


def check_move(db, campaign_candidate, from_stage: PipelineStage, to_stage: PipelineStage, *,
               actor_id: str | None, actor_role: str | None, reason_code: str | None,
               candidates_in_batch: int = 1) -> CandidateOutcomeOverride | None:
    """
    Before a person's stage move: the override row to record (not added yet), or None when the move
    doesn't go against the outcome. Raises a 422 (data.code OVERRIDE_REASON_REQUIRED) when it does
    and no reason code was given, or OVERRIDE_REASON_UNKNOWN for a code not in REASON_CODES.
    """
    if actor_id is None:
        return None
    result = acted_on_result(db, campaign_candidate.id)
    if result is None:
        return None
    outcome = normal_outcome(result.outcome)
    kind_direction = classify(outcome, from_stage, to_stage)
    if kind_direction is None:
        return None
    kind, direction = kind_direction
    if not reason_code:
        what = "goes against the system's outcome" if kind == KIND_OVERRIDE else "resolves a HOLD"
        raise CampaignException(
            f"This move {what} ({outcome}): choose a reason.", 422,
            data={"code": REQUIRED_CODE, "kind": kind, "direction": direction, "system_outcome": outcome,
                  "from_stage": from_stage.value, "to_stage": to_stage.value,
                  "candidates_in_batch": candidates_in_batch, "reason_codes": reason_code_options()})
    if reason_code not in REASON_CODES:
        raise CampaignException(f"Unknown override reason code '{reason_code}'.", 422,
                                data={"code": UNKNOWN_CODE, "reason_codes": reason_code_options()})
    breakdown = result.breakdown or {}
    return CandidateOutcomeOverride(
        campaign_candidate_id=campaign_candidate.id, campaign_id=campaign_candidate.campaign_id,
        scoring_result_id=result.id, scoring_version=result.scoring_version,
        scoring_config_version=result.scoring_config_version, system_outcome=outcome,
        kind=kind, direction=direction, from_stage=from_stage, to_stage=to_stage, reason_code=reason_code,
        failed_checks=list(breakdown.get("failed_checks") or []) or None,
        missing_core_skills=_missing_core(breakdown) or None,
        profile_template=_template(breakdown),
        actor_id=actor_id, actor_role=actor_role,
    )
