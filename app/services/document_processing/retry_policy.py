from dataclasses import dataclass

from app.models.async_tasks import ProcessingStage


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: int
    max_delay_seconds: int


DEFAULT_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=5, max_delay_seconds=60)

STAGE_POLICIES = {
    ProcessingStage.AI_EXTRACTION: RetryPolicy(max_attempts=5, base_delay_seconds=10, max_delay_seconds=120),
}

STAGE_ORDER = [
    ProcessingStage.VALIDATION,
    ProcessingStage.STORAGE,
    ProcessingStage.TEXT_EXTRACTION,
    ProcessingStage.TEXT_CLEANING,
    ProcessingStage.PII_DETECTION,
    ProcessingStage.PII_REDACTION,
    ProcessingStage.AI_EXTRACTION,
    ProcessingStage.JSON_VALIDATION,
    ProcessingStage.SKILL_NORMALIZATION,
    ProcessingStage.EMBEDDING_GENERATION,
    ProcessingStage.PERSISTENCE,
]


def compute_backoff_seconds(policy: RetryPolicy, attempt_number: int) -> int:
    return min(policy.base_delay_seconds * (2 ** (attempt_number - 1)), policy.max_delay_seconds)


def get_max_attempts(stage: ProcessingStage | None) -> int:
    """
    Single, named lookup for a stage's configured retry ceiling — the same
    STAGE_POLICIES/DEFAULT_POLICY fallback RetryDriver.handle_failure
    already applies inline, exposed here so a read-only caller (e.g. a
    future retry-summary API) doesn't need to duplicate that fallback
    rule itself. RetryDriver's own inline lookup is untouched — this is a
    parallel accessor, not a replacement, so retry behavior cannot change.
    stage=None (no stage-execution row exists yet) falls back to
    DEFAULT_POLICY, same as an unrecognized stage would.
    """
    if stage is None:
        return DEFAULT_POLICY.max_attempts
    return STAGE_POLICIES.get(stage, DEFAULT_POLICY).max_attempts


def get_retries_remaining(stage: ProcessingStage | None, attempt_number: int) -> int:
    """
    How many further attempts a stage still has, given the attempt it is
    on. Mirrors RetryDriver.handle_failure's `attempt_number >=
    policy.max_attempts` give-up rule exactly, so a client showing
    "retry 2 of 3 - 1 left" can never disagree with what the driver will
    actually do next.

    Read-only, like get_max_attempts above: it re-derives the budget from
    the same policy tables rather than changing how retries are decided.
    Note this is the *budget* left, not a promise - a PERMANENT failure
    classification dead-letters immediately with attempts still unused,
    which is why callers surfacing a terminal status (SUCCESS/FAILURE/
    DEAD) should report 0 rather than this number.
    """
    return max(0, get_max_attempts(stage) - attempt_number)
