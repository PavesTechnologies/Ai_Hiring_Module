"""
REVIEW outcome: near-miss candidates go to HOLD ("Needs recruiter review")
instead of being rejected. Shared by v1 (live decision), v2 and the AI step.

A near-miss is one of:
  - a core skill over the missing-core limit - counted as
    max(0, definite_missing + uncertain - allowed); an uncertain core match
    (v2 only) only matters when it is what takes the candidate over the limit
  - experience short of the JD minimum by no more than
    max(1.0 year, 10% of the minimum)

Exactly one near-miss -> HOLD; two or more -> REJECT. Any other failed check
(coverage, education, score threshold where it applies), experience short by
more than the tolerance, or zero confirmed core skills (on a JD that has core
skills) -> REJECT. Semantic and AI still run for a HOLD candidate and may
still reject; their best outcome is capped at HOLD.
"""
from dataclasses import dataclass, field

OUTCOME_PASS = "PASS"
OUTCOME_HOLD = "HOLD"
OUTCOME_REJECT = "REJECT"

NEEDS_REVIEW_REASON = "Needs recruiter review"
EXPERIENCE_NEAR_MISS_MIN_YEARS = 1.0
EXPERIENCE_NEAR_MISS_PCT = 0.10


def needs_recruiter_review(campaign_candidate) -> bool:
    """True when the stored deterministic decision was a near-miss (REVIEW) - the AI step caps it at HOLD."""
    breakdown = getattr(campaign_candidate, "deterministic_breakdown", None) or {}
    return isinstance(breakdown, dict) and breakdown.get("needs_review") is True


def experience_near_miss_tolerance(min_years: float) -> float:
    """How far short of the JD minimum still counts as a near-miss: max(1.0 year, 10% of the minimum)."""
    return max(EXPERIENCE_NEAR_MISS_MIN_YEARS, EXPERIENCE_NEAR_MISS_PCT * float(min_years))


@dataclass
class NearMissDecision:
    outcome: str
    near_misses: list[str] = field(default_factory=list)
    reject_reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.outcome == OUTCOME_HOLD

    @property
    def review_reason(self) -> str | None:
        return f"{NEEDS_REVIEW_REASON}: {'; '.join(self.near_misses)}" if self.needs_review else None

    def as_breakdown(self) -> dict:
        return {
            "near_miss_outcome": self.outcome,
            "needs_review": self.needs_review,
            "needs_review_reason": self.review_reason,
            "near_misses": list(self.near_misses),
            "near_miss_reject_reasons": list(self.reject_reasons),
        }


def evaluate_near_miss(
    *,
    core_skill_count: int,
    confirmed_core_count: int,
    allowed_missing_core: int,
    definite_missing_core: int,
    uncertain_core: int = 0,
    over_limit_names: list[str] | None = None,
    experience: dict | None = None,
    other_failed_checks: list[str] | None = None,
) -> NearMissDecision:
    """
    The single decision rule. other_failed_checks are checks that are never a
    near-miss (coverage, education, score threshold); any of them rejects.
    """
    near_misses: list[str] = []
    near_miss_count = 0   # each core skill over the limit counts once
    reject_reasons = [f"{name} not met" for name in (other_failed_checks or [])]

    if core_skill_count > 0:
        over = max(0, definite_missing_core + uncertain_core - allowed_missing_core)
        if confirmed_core_count == 0:
            reject_reasons.append("No core skill confirmed")
        elif over:
            names = f" (missing: {', '.join(over_limit_names)})" if over_limit_names else ""
            uncertain_note = f", {uncertain_core} uncertain" if uncertain_core else ""
            label = "core skill" if over == 1 else "core skills"
            near_misses.append(f"{over} {label} over the missing-core limit of {allowed_missing_core}"
                               f"{uncertain_note}{names}")
            near_miss_count += over

    if experience and experience.get("applicable") and experience.get("passed") is False:
        years, min_years = experience.get("candidate_years"), experience.get("min_years")
        if years is None or min_years is None:
            reject_reasons.append("Experience not met")
        else:
            shortfall = float(min_years) - float(years)
            tolerance = experience_near_miss_tolerance(float(min_years))
            if shortfall <= tolerance + 1e-9:
                near_miss_count += 1
                near_misses.append(
                    f"experience {float(years):g} years, {shortfall:.1f} short of the {float(min_years):g}-year "
                    f"minimum (within the {tolerance:g}-year tolerance)"
                )
            else:
                reject_reasons.append(
                    f"Experience {float(years):g} years is {shortfall:.1f} short of the {float(min_years):g}-year "
                    f"minimum (more than the {tolerance:g}-year tolerance)"
                )

    if reject_reasons:
        return NearMissDecision(OUTCOME_REJECT, near_misses, reject_reasons)
    if near_miss_count == 0:
        return NearMissDecision(OUTCOME_PASS)
    if near_miss_count == 1:
        return NearMissDecision(OUTCOME_HOLD, near_misses)
    return NearMissDecision(OUTCOME_REJECT, near_misses,
                            [f"{near_miss_count} near-misses - only one is allowed"])
