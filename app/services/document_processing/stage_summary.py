"""
Pure, document-type-agnostic helpers for presenting a processing run's
stage rows and retry budget.

JD and Resume deliberately keep separate context/pipeline/status classes
(see JDProcessingContext's docstring for why), and that stays true — what
is shared here is not behaviour or state but two stateless calculations
over DocumentProcessingStageExecution rows, which are the *same table* for
both document types. Duplicating them was the alternative, and a drift
between the two copies would show users different progress and different
retry numbers for identical underlying data.
"""

from app.models.async_tasks import StageExecutionStatus, TaskStatus
from app.services.document_processing.retry_policy import (
    STAGE_ORDER,
    get_max_attempts,
    get_retries_remaining,
)

# A task in one of these states will never be retried again, so its
# retries_remaining is 0 regardless of the budget its last stage had left
# — a PERMANENT classification dead-letters with attempts unused, and a
# SUCCESS obviously consumes none. Reporting the raw budget here is what
# would let a finished upload claim "1 retry left" forever.
_TERMINAL_STATUSES = {TaskStatus.SUCCESS, TaskStatus.FAILURE, TaskStatus.DEAD}


def collapse_to_latest_attempt(executions) -> list:
    """
    One row per stage — the newest meaningful attempt of each — ordered by
    STAGE_ORDER (the real pipeline order), not by insertion.

    Every retry re-records the whole prefix of the pipeline, so a task that
    retried 5 times returns 17 rows of which 12 are SKIPPED repeats of work
    already shown. That forces every client to de-duplicate identically
    before it can render a progress list, and the raw list grows by ~3 rows
    per retry.

    A SKIPPED row is never preferred over a real outcome for the same
    stage. SKIPPED means "already completed on an earlier attempt, resumed
    from checkpoint" — so on attempt 5, TEXT_EXTRACTION's newest row is a
    SKIP even though the stage genuinely SUCCEEDED on attempt 1. Taking the
    newest row blindly would show a finished stage as "skipped" and drop
    its real duration_ms (SKIPPED rows have none). Picking the
    highest-attempt non-SKIPPED row keeps the outcome and the timing, and
    falls back to a SKIPPED row only when that is all there is.
    """
    latest_by_stage: dict = {}
    for execution in executions:
        current = latest_by_stage.get(execution.stage)
        if current is None:
            latest_by_stage[execution.stage] = execution
            continue

        current_is_skip = current.status == StageExecutionStatus.SKIPPED
        incoming_is_skip = execution.status == StageExecutionStatus.SKIPPED
        if current_is_skip and not incoming_is_skip:
            latest_by_stage[execution.stage] = execution
        elif incoming_is_skip and not current_is_skip:
            continue
        elif execution.attempt_number >= current.attempt_number:
            latest_by_stage[execution.stage] = execution

    return sorted(
        latest_by_stage.values(),
        key=lambda e: STAGE_ORDER.index(e.stage) if e.stage in STAGE_ORDER else len(STAGE_ORDER),
    )


def retry_budget(task_log, collapsed_executions) -> tuple[int, int]:
    """
    Task-level (max_attempts, retries_remaining).

    The ceiling comes from the stage the task is actually sitting on, not a
    constant, because STAGE_POLICIES lets AI_EXTRACTION run to 5 attempts
    where every other stage stops at 3 — a task retrying in AI_EXTRACTION
    genuinely has a bigger budget than one retrying in PERSISTENCE, and
    flattening that would mislead.

    retry_count on the log counts retries, so the attempt in progress is
    retry_count + 1.
    """
    current_stage = collapsed_executions[-1].stage if collapsed_executions else None
    max_attempts = get_max_attempts(current_stage)
    if task_log.status in _TERMINAL_STATUSES:
        return max_attempts, 0
    return max_attempts, get_retries_remaining(current_stage, (task_log.retry_count or 0) + 1)


def stage_retries_remaining(execution) -> int | None:
    """
    Per-stage "attempts left", non-null only on the stage that actually
    failed — that is the stage whose policy decided the retry, so it is the
    only row where the number answers a real question.

    Comparing a SKIPPED or SUCCESS row's pipeline-wide attempt_number
    against its own max_attempts produces a confident-looking lie
    (TEXT_EXTRACTION SKIPPED on attempt 5 vs. its ceiling of 3 = "0 left",
    when nothing about TEXT_EXTRACTION was retried at all).
    """
    if execution.status != StageExecutionStatus.FAILED:
        return None
    return get_retries_remaining(execution.stage, execution.attempt_number)
