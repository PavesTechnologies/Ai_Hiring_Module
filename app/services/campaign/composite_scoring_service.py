import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.enums.constants import ActionType, COMPOSITE_SCORE_FORMULA_VERSION, EntityType
from app.models.pipeline import CandidateCompositeScoreHistory, CompositeScoreTriggerSource
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)
from app.services.audit_service import AuditService
from app.utils.scoring_utils import round_composite_score

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100.00")
_ZERO = Decimal("0")


def _effective_ai_score(campaign_candidate):
    """effective_ai_score now lives on the related CampaignCandidateAIEvaluation row (1:1)."""
    return campaign_candidate.ai_evaluation.effective_ai_score if campaign_candidate.ai_evaluation else None


class InvalidScoringWeightsError(ValueError):
    """
    A campaign's weight_deterministic + weight_semantic + weight_ai does not
    sum to 100.00. Deliberately a ValueError subclass - this is a genuine
    permanent/data-integrity failure that must never be retried:
    error_classifier.classify() marks ValueError PERMANENT, which is exactly
    right here (the DB's own chk_weights_sum_100 constraint should make this
    unreachable in practice; this is a defensive re-check, not the primary
    guard - CompositeScoringService never relies on upstream validation
    alone).
    """


class InvalidScoreRangeError(ValueError):
    """
    One of deterministic_score (expected 0-100), semantic_score (expected
    0-1) or effective_ai_score (expected 0-100) is outside its valid range.
    A ValueError subclass for the same reason as InvalidScoringWeightsError -
    a genuine data-integrity failure, permanent, never retried.
    """


class CompositeScoringService:
    """
    M10-E01: computes campaign_candidates.composite_score from whichever of
    deterministic_score / semantic_score / effective_ai_score are present
    on a campaign_candidate, weighted by the owning campaign's
    weight_deterministic/weight_semantic/weight_ai - used exactly as
    configured, with no redistribution. A missing score component is
    treated as 0 (COALESCE semantics), never excluded from the weighting.

    CompositeScoringService is the SINGLE SOURCE OF TRUTH for
    campaign_candidates.composite_score - no other service, repository,
    API, helper or Celery task may write that column (or insert into
    candidate_composite_score_history). Every other trigger site must call
    into this service (via the Celery task) rather than computing or
    writing a composite score itself.

    Composite Score has exactly two valid triggers (see
    CompositeScoreTriggerSource): AI Evaluation completing, and a
    campaign's scoring weights changing. Never resume upload/parsing/
    reprocessing/reset, a deterministic/semantic completion, or an HR
    override - an HR override only restarts the remaining scoring pipeline
    (deterministic re-pass -> semantic -> AI evaluation); it is that
    eventual AI evaluation completing which (re)triggers Composite Score.

    Mirrors SemanticScoringService's overall shape (injected repositories,
    no commit of its own - that belongs to the caller), extended per this
    epic's explicit design with audit logging and append-only history
    insertion as first-class responsibilities of the service itself.
    Deliberately never recomputes deterministic_score, semantic_score or
    effective_ai_score - those are read exactly as already stored by their
    own scoring layers/AI evaluation.

    Broken into one method per responsibility (validate_inputs,
    normalize_scores, calculate_score, round_score, persist,
    create_history, write_audit) rather than one large method, each
    independently testable.
    """

    def __init__(
        self,
        campaign_candidate_repository: CampaignCandidateRepository,
        campaign_repository: CampaignRepository,
        composite_score_history_repository: CandidateCompositeScoreHistoryRepository,
        audit_service: AuditService,
    ):
        self.campaign_candidate_repository = campaign_candidate_repository
        self.campaign_repository = campaign_repository
        self.composite_score_history_repository = composite_score_history_repository
        self.audit_service = audit_service

    def calculate_and_store_composite_score(
        self,
        campaign_candidate_id: UUID,
        trigger_source: CompositeScoreTriggerSource,
    ) -> dict:
        """
        Orchestrates the full calculation: fetch + validate, normalize,
        calculate, round, persist, record history, audit. Does not commit -
        that is the caller's responsibility (same convention as every other
        scoring service in this codebase), so a validation failure can be
        rolled back as one atomic unit by the caller with no partial
        writes.
        """
        campaign_candidate, campaign = self.validate_inputs(campaign_candidate_id)

        weights = {
            "deterministic": Decimal(str(campaign.weight_deterministic)),
            "semantic": Decimal(str(campaign.weight_semantic)),
            "ai": Decimal(str(campaign.weight_ai)),
        }
        normalized_scores = self.normalize_scores(campaign_candidate)

        composite_score_precise = self.calculate_score(weights, normalized_scores)
        composite_score = self.round_score(composite_score_precise)

        now = datetime.now(timezone.utc)
        breakdown = {
            "deterministic_score": campaign_candidate.deterministic_score,
            "semantic_score": campaign_candidate.semantic_score,
            "normalized_semantic_score": (
                float(normalized_scores["semantic_normalized"])
                if campaign_candidate.semantic_score is not None else None
            ),
            "effective_ai_score": _effective_ai_score(campaign_candidate),
            "weight_deterministic": float(weights["deterministic"]),
            "weight_semantic": float(weights["semantic"]),
            "weight_ai": float(weights["ai"]),
            "composite_score": composite_score,
            "formula_version": COMPOSITE_SCORE_FORMULA_VERSION,
            "trigger_source": trigger_source.value,
            "computed_at": now.isoformat(),
        }

        self.persist(campaign_candidate, composite_score, now)
        self.create_history(campaign_candidate, weights, normalized_scores, composite_score, trigger_source)
        self.write_audit(campaign_candidate, campaign, breakdown)

        return breakdown

    def validate_inputs(self, campaign_candidate_id: UUID) -> tuple:
        """
        Fetches and validates every precondition BEFORE any score is read
        or any computation happens - CompositeScoringService never relies
        on upstream validation alone (Design Decision: Defensive
        Validation). On any failure: nothing has been written yet, so the
        caller's rollback is a no-op cleanup, not an undo of partial work.

        Validates, in order: candidate exists, campaign exists, campaign
        weights sum to exactly 100.00, and each present score component is
        within its valid range (deterministic_score/effective_ai_score:
        0-100, semantic_score: 0-1).
        """
        campaign_candidate = self.campaign_candidate_repository.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise ValueError(f"CampaignCandidate '{campaign_candidate_id}' not found.")

        campaign = self.campaign_repository.get_by_id(campaign_candidate.campaign_id)
        if campaign is None:
            raise ValueError(f"Campaign '{campaign_candidate.campaign_id}' not found.")

        self._validate_weights(campaign)
        self._validate_score_ranges(campaign_candidate)

        return campaign_candidate, campaign

    @staticmethod
    def _validate_weights(campaign) -> None:
        """
        Weight validation: weight_deterministic + weight_semantic +
        weight_ai must equal exactly 100.00 before any computation runs.
        Backed by the DB's own chk_weights_sum_100 CHECK constraint (this
        should be unreachable in practice) - this is a defensive re-check
        so composite scoring never silently computes against corrupt
        weights; on failure, logs the error and raises so the caller rolls
        back the whole transaction with no partial writes.
        """
        total = (
            Decimal(str(campaign.weight_deterministic))
            + Decimal(str(campaign.weight_semantic))
            + Decimal(str(campaign.weight_ai))
        )
        if total != _HUNDRED:
            logger.error(
                "Composite score computation aborted | campaign_id=%s reason=invalid_weights "
                "weight_deterministic=%s weight_semantic=%s weight_ai=%s total=%s",
                campaign.id, campaign.weight_deterministic, campaign.weight_semantic, campaign.weight_ai, total,
            )
            raise InvalidScoringWeightsError(
                f"Campaign '{campaign.id}' scoring weights sum to {total}, not 100.00 - "
                "composite score cannot be computed.",
            )

    @staticmethod
    def _validate_score_ranges(campaign_candidate) -> None:
        """
        Defensive range validation - CompositeScoringService never relies
        solely on each layer's own upstream validation/DB constraints.
        deterministic_score and effective_ai_score are expected 0-100;
        semantic_score (a raw cosine similarity) is expected 0-1. None
        (not yet scored) is always valid and is handled by
        normalize_scores' COALESCE-to-0 semantics, not here.
        """
        checks = (
            ("deterministic_score", campaign_candidate.deterministic_score, Decimal("0"), Decimal("100")),
            ("semantic_score", campaign_candidate.semantic_score, Decimal("0"), Decimal("1")),
            ("effective_ai_score", _effective_ai_score(campaign_candidate), Decimal("0"), Decimal("100")),
        )
        for field_name, value, min_value, max_value in checks:
            if value is None:
                continue
            decimal_value = Decimal(str(value))
            if decimal_value < min_value or decimal_value > max_value:
                logger.error(
                    "Composite score computation aborted | campaign_candidate_id=%s reason=invalid_score_range "
                    "field=%s value=%s expected_range=[%s, %s]",
                    campaign_candidate.id, field_name, value, min_value, max_value,
                )
                raise InvalidScoreRangeError(
                    f"CampaignCandidate '{campaign_candidate.id}' field '{field_name}' has value {value}, "
                    f"outside the expected range [{min_value}, {max_value}] - composite score cannot be computed.",
                )

    @staticmethod
    def normalize_scores(campaign_candidate) -> dict:
        """
        Story 2/4: a missing score component is COALESCEd to 0, never
        excluded - campaign weights are used exactly as configured, with
        no redistribution. semantic_score is stored as a 0-1 cosine
        similarity (Numeric(7,6)); it is rescaled to the same 0-100 scale
        as deterministic_score/effective_ai_score for combination purposes
        only - the raw value is what is persisted to breakdown/history.
        """
        deterministic = (
            Decimal(str(campaign_candidate.deterministic_score))
            if campaign_candidate.deterministic_score is not None else _ZERO
        )
        semantic_normalized = (
            Decimal(str(campaign_candidate.semantic_score)) * _HUNDRED
            if campaign_candidate.semantic_score is not None else _ZERO
        )
        ai_score = _effective_ai_score(campaign_candidate)
        ai = Decimal(str(ai_score)) if ai_score is not None else _ZERO
        return {
            "deterministic": deterministic,
            "semantic_normalized": semantic_normalized,
            "ai": ai,
        }

    @staticmethod
    def calculate_score(weights: dict, normalized_scores: dict) -> Decimal:
        """
        composite_score = Σ (weight[c] / 100) * normalized_score[c], over
        the three components, using the campaign's weights EXACTLY as
        configured (no redistribution - weights always sum to 100.00,
        already validated by validate_inputs). Decimal throughout - no
        rounding happens here, only in round_score.
        """
        return (
            (weights["deterministic"] / _HUNDRED) * normalized_scores["deterministic"]
            + (weights["semantic"] / _HUNDRED) * normalized_scores["semantic_normalized"]
            + (weights["ai"] / _HUNDRED) * normalized_scores["ai"]
        )

    @staticmethod
    def round_score(composite_score_precise: Decimal) -> float:
        """
        The ONLY rounding step in the whole calculation - every value
        feeding into composite_score_precise stays full-precision Decimal
        up to this point.
        """
        return round_composite_score(composite_score_precise)

    def persist(self, campaign_candidate, composite_score: float, computed_at: datetime) -> None:
        """
        Writes campaign_candidates.composite_score +
        composite_score_computed_at. CompositeScoringService is the only
        writer of composite_score anywhere in the codebase.
        """
        campaign_candidate.composite_score = composite_score
        campaign_candidate.composite_score_computed_at = computed_at
        self.campaign_candidate_repository.update(campaign_candidate)

    def create_history(
        self,
        campaign_candidate,
        weights: dict,
        normalized_scores: dict,
        composite_score: float,
        trigger_source: CompositeScoreTriggerSource,
    ) -> None:
        """Inserts one immutable candidate_composite_score_history row - never an update."""
        self.composite_score_history_repository.create(CandidateCompositeScoreHistory(
            campaign_candidate_id=campaign_candidate.id,
            deterministic_score=campaign_candidate.deterministic_score,
            semantic_score=campaign_candidate.semantic_score,
            normalized_semantic_score=(
                normalized_scores["semantic_normalized"] if campaign_candidate.semantic_score is not None else None
            ),
            effective_ai_score=_effective_ai_score(campaign_candidate),
            weight_deterministic=weights["deterministic"],
            weight_semantic=weights["semantic"],
            weight_ai=weights["ai"],
            composite_score=composite_score,
            formula_version=COMPOSITE_SCORE_FORMULA_VERSION,
            trigger_source=trigger_source,
        ))

    def write_audit(self, campaign_candidate, campaign, breakdown: dict) -> None:
        """One COMPOSITE_SCORE_COMPUTED audit log entry per successful calculation."""
        self.audit_service.log(
            actor_id=None,
            actor_role="SYSTEM",
            action_type=ActionType.COMPOSITE_SCORE_COMPUTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign.id,
            details=breakdown,
        )
