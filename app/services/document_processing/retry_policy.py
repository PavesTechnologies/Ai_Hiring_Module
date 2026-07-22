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
