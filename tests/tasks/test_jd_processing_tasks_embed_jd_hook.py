"""
Focused coverage for the EMBED_JD auto-enqueue hook added to
process_jd_document - not a full test of the JD processing pipeline itself
(pipeline.run() is mocked outright), just that a successful run enqueues
EMBED_JD afterward, and that an enqueue failure never crashes or masks the
already-successful JD creation/reprocess.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

TASKS_MODULE = "app.tasks.jd_processing_tasks"


class _Harness:
    def __init__(self):
        self.jd_repo = MagicMock()
        self.skill_repo = MagicMock()
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None

        def _create(log):
            log.retry_count = getattr(log, "retry_count", 0) or 0
            return log

        self.task_log_repo.create.side_effect = _create
        self.task_log_repo.update.side_effect = lambda log: log
        self.pipeline_instance = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.SkillRepository", return_value=self.skill_repo),
            patch(f"{TASKS_MODULE}.DocumentProcessingRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.StageExecutionService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.JDService", return_value=MagicMock(JD_STORAGE_BUCKET="bucket", storage_service=MagicMock())),
            patch(f"{TASKS_MODULE}.EmbeddingService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CheckpointRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.StageFailureLogRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.RetryDriver", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.PreprocessingService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.GeminiExtractionService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.HashService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.StorageService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.PromptTemplateRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.SkillNormalizationService", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.JDProcessingPipeline", return_value=self.pipeline_instance),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _base_kwargs(task_id=None):
    return dict(
        task_id=task_id or str(uuid4()),
        raw_text="Some JD text.",
        file_path=None,
        title="Backend Engineer",
        jurisdiction="US",
        min_experience_years=None,
        education_criteria=None,
        created_by="hr_user",
        prompt_template_id=str(uuid4()),
    )


def test_enqueues_embed_jd_after_successful_creation():
    from app.tasks.jd_processing_tasks import process_jd_document

    with _Harness() as h:
        jd_id = uuid4()
        h.pipeline_instance.run.return_value = jd_id

        with patch("app.services.embedding_queue_service.EmbeddingQueueService") as queue_service_class:
            process_jd_document(**_base_kwargs())

            queue_service_class.return_value.queue_jd_embedding.assert_called_once_with(
                jd_id, force_regenerate=False,
            )


def test_embed_jd_enqueue_failure_never_crashes_or_masks_successful_creation():
    from app.services.embedding_queue_service import JDEmbeddingQueueError
    from app.tasks.jd_processing_tasks import process_jd_document

    with _Harness() as h:
        jd_id = uuid4()
        h.pipeline_instance.run.return_value = jd_id

        with patch("app.services.embedding_queue_service.EmbeddingQueueService") as queue_service_class:
            queue_service_class.return_value.queue_jd_embedding.side_effect = JDEmbeddingQueueError(
                "broker unreachable", jd_id=jd_id, task_id=uuid4(),
            )

            # Must not raise - JD creation already succeeded and committed.
            process_jd_document(**_base_kwargs())

        task_log = h.task_log_repo.update.call_args_list[-1].args[0]
        from app.models.async_tasks import TaskStatus
        assert task_log.status == TaskStatus.SUCCESS
