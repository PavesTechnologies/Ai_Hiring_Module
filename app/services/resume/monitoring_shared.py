from app.models.async_tasks import FailureClassification, StageExecutionStatus
from app.schemas.resume.monitoring import FailureInfo, StageExecutionDetail

# Fixed denominator for progress_percent — the 7 real, always-tracked stages
# both upload flows share (STORAGE/VALIDATION are not independently tracked
# by either pipeline; see docs/Resume_Intake_Monitoring_API_Design.md).
TRACKED_STAGE_COUNT = 7

CLASSIFICATION_TO_RETRYABLE = {
    FailureClassification.TRANSIENT: True,
    FailureClassification.PERMANENT: False,
    FailureClassification.UNKNOWN: None,
}


def build_stage_timeline_fields(
    task_id: str,
    task_log,
    executions,
    failure_logs,
    attempt_number: int | None,
) -> dict:
    """
    Shared by ResumeMonitoringService.get_timeline and
    BulkUploadMonitoringService.get_file_timeline — endpoints #2 and #7 are
    defined in docs/Resume_Intake_Monitoring_API_Design.md as sharing an
    identical StageTimeline shape, so the field-building logic is shared
    here rather than kept as two independently-drifting copies. Returns the
    kwargs common to both ResumeTimelineResponse and BulkFileTimelineResponse
    (everything except the caller-supplied identity field).
    """
    target_attempt = attempt_number if attempt_number is not None else task_log.retry_count + 1

    filtered_executions = [e for e in executions if e.attempt_number == target_attempt]
    retryable_by_key = {
        (failure.stage, failure.attempt_number): CLASSIFICATION_TO_RETRYABLE.get(failure.classification)
        for failure in failure_logs
    }

    stages = [
        StageExecutionDetail(
            stage=execution.stage.value,
            status=execution.status.value,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            duration_ms=execution.duration_ms,
            attempt_number=execution.attempt_number,
            error_message=execution.error_message,
            skipped=execution.status == StageExecutionStatus.SKIPPED,
            retryable=(
                retryable_by_key.get((execution.stage, execution.attempt_number))
                if execution.status == StageExecutionStatus.FAILED
                else None
            ),
        )
        for execution in filtered_executions
    ]

    done_count = sum(
        1 for s in stages if s.status in (StageExecutionStatus.SUCCESS.value, StageExecutionStatus.SKIPPED.value)
    )

    return dict(
        task_id=task_id,
        document_type="RESUME",
        overall_status=task_log.status.value,
        current_stage=stages[-1].stage if stages else None,
        attempt_number=target_attempt,
        retry_count=task_log.retry_count,
        progress_percent=round(done_count / TRACKED_STAGE_COUNT * 100, 1),
        queued_at=task_log.queued_at,
        started_at=task_log.started_at,
        completed_at=task_log.completed_at,
        stages=stages,
    )


def build_failure_info(executions, failure_logs, dlq_entry, task_log) -> FailureInfo:
    """
    Shared by ResumeMonitoringService.get_resume_detail and
    BulkUploadMonitoringService.get_file_detail — endpoint #6 is defined as
    mirroring endpoint #1's detail shape, including the failure sub-object.
    """
    failed_execution = next(
        (e for e in reversed(executions) if e.status == StageExecutionStatus.FAILED), None,
    )
    matching_failure = None
    if failed_execution is not None:
        matching_failure = next(
            (
                f for f in reversed(failure_logs)
                if f.stage == failed_execution.stage and f.attempt_number == failed_execution.attempt_number
            ),
            None,
        )
    return FailureInfo(
        failed_stage=failed_execution.stage.value if failed_execution else None,
        error_message=task_log.error_message if task_log else None,
        classification=matching_failure.classification.value if matching_failure else None,
        moved_to_dlq=dlq_entry is not None,
    )
