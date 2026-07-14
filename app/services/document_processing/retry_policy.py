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
