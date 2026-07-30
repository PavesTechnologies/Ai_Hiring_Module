import logging
from uuid import UUID

from app.models.async_tasks import ProcessingStage
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.repositories.resume_repository import ResumeRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.ai.resume_extraction_response import ResumeExtractionGenerationSchema, ResumeExtractionResponse
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_redaction_service import PIIRedactionService
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.resume import resume_embedding_text_builder
from app.services.resume.resume_processing_context import ResumeProcessingContext
from app.services.resume.resume_service import ResumeService
from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService

logger = logging.getLogger(__name__)


class ResumeProcessingPipeline:
    """
    Orchestrates the Resume document-processing pipeline: Text Extraction ->
    Text Cleaning -> AI Extraction -> JSON Validation -> Skill Normalization
    -> Persistence. Mirrors JDProcessingPipeline's stage loop and
    StageExecutionService usage, with one deliberate deviation: stages are
    run WITHOUT `context=`/`checkpoint_repo=` args.

    M08-E01: embedding generation is no longer a stage of this pipeline -
    it's now EMBED_RESUME, a separate, decoupled Celery task
    (app/tasks/embedding_tasks.py) enqueued after this pipeline succeeds,
    mirroring exactly how DETERMINISTIC_SCORE is already enqueued after
    resume processing completes. This avoids a resume ever getting two
    embedding rows (one from this pipeline, one from EMBED_RESUME) and
    gives embedding generation its own dedup/anonymisation-verification/
    task-log pipeline independent of parsing.

    StageExecutionService.run_stage's failure branch calls
    app.services.jd.context_serializer.to_dict(context) unconditionally
    (that import is hardcoded, not dispatched by document_type) — passing a
    ResumeProcessingContext into it would crash on the first failed stage,
    since that serializer reads JD-only attributes (title, extraction,
    jd_id, ...). Omitting context/checkpoint_repo keeps per-stage
    success/failure logging (StageExecutionService itself) working exactly
    like JD's, at the cost of mid-run checkpoint resume: a retried Celery
    task for a resume re-runs every stage from TEXT_EXTRACTION rather than
    resuming from the failed stage. Fixing this properly means making
    StageExecutionService dispatch its serializer by document_type, which
    is out of scope here (JD-facing shared file, not the one permitted
    change).

    Concrete and Resume-specific by design, same reasoning as
    JDProcessingPipeline's own docstring.
    """

    RESUME_STORAGE_BUCKET = ResumeService.RESUME_STORAGE_BUCKET

    def __init__(
        self,
        *,
        preprocessing_service: PreprocessingService,
        extraction_service: GeminiExtractionService,
        storage_service: StorageService,
        skill_normalization_service: SkillNormalizationService,
        resume_service: ResumeService,
        resume_repository: ResumeRepository,
        skill_repository: SkillRepository,
        stage_tracker: StageExecutionService,
        pii_detection_service: PIIDetectionService,
        pii_redaction_service: PIIRedactionService,
        prompt_template_repository: PromptTemplateRepository,
    ):
        self.preprocessing_service = preprocessing_service
        self.extraction_service = extraction_service
        self.storage_service = storage_service
        self.skill_normalization_service = skill_normalization_service
        self.resume_service = resume_service
        self.resume_repository = resume_repository
        self.skill_repository = skill_repository
        self.stage_tracker = stage_tracker
        self.pii_detection_service = pii_detection_service
        self.pii_redaction_service = pii_redaction_service
        self.prompt_template_repository = prompt_template_repository

    def run(
        self,
        *,
        task_id: str,
        resume_id: UUID,
        candidate_id: UUID,
        file_path: str,
        source_format: ResumeSourceFormat,
        prompt_template_id: UUID,
        attempt_number: int = 1,
        initial_context: ResumeProcessingContext | None = None,
    ) -> UUID:
        """
        initial_context lets a caller that already ran some of these stages
        itself (bulk upload, which must extract text and run AI extraction
        before Candidate/Resume — and therefore this context — can exist)
        hand in a context with those stages' outputs already populated.

        A stage whose expected output is already present on the context is
        skipped entirely — not re-run, and no skip_stage() call either. The
        caller that populated the context (bulk's own pre-identity
        stage_tracker.run_stage() calls) already wrote the real SUCCESS
        document_processing_stage_executions row for it in this same
        attempt; calling skip_stage() here would try to write a second
        record for the exact same (task_id, stage, attempt_number) and
        overwrite that real row with a SKIPPED one instead. This differs
        from JDProcessingPipeline's _should_skip_stage/skip_stage pattern,
        which exists for a genuinely different case (a checkpoint-resumed
        retry where the stage truly did not run in this attempt) — bulk's
        case is "already ran, just not through this method," not "never
        ran." An individual-upload call never passes initial_context, so
        every stage always runs exactly as before this change.
        """
        context = initial_context or ResumeProcessingContext(
            task_id=task_id,
            file_path=file_path,
            source_format=source_format,
        )
        context.resume_id = resume_id
        context.candidate_id = candidate_id
        context.prompt_template_id = prompt_template_id
        context.attempt_number = attempt_number

        for stage, output_attr, fn in (
            (ProcessingStage.TEXT_EXTRACTION, "raw_text", lambda: self._run_text_extraction(context)),
            (ProcessingStage.TEXT_CLEANING, "cleaned_text", lambda: self._run_text_cleaning(context)),
            (ProcessingStage.PII_DETECTION, "pii_findings", lambda: self._run_pii_detection(context)),
            (ProcessingStage.PII_REDACTION, "redacted_text", lambda: self._run_pii_redaction(context)),
            (ProcessingStage.AI_EXTRACTION, "raw_extraction", lambda: self._run_ai_extraction(context)),
            (ProcessingStage.JSON_VALIDATION, "validated_extraction", lambda: self._run_json_validation(context)),
            (ProcessingStage.SKILL_NORMALIZATION, "skill_match_results", lambda: self._run_skill_normalization(context)),
            (ProcessingStage.PERSISTENCE, None, lambda: self._run_persistence(context)),
        ):
            logger.warning("=== STAGE STARTING: %s === resume_id=%s", stage.value, context.resume_id)
            if output_attr is not None and getattr(context, output_attr) is not None:
                continue
            self.stage_tracker.run_stage(
                context.task_id,
                context.document_type,
                stage,
                fn,
                attempt_number=attempt_number,
            )
            logger.warning("=== STAGE COMPLETED: %s === resume_id=%s", stage.value, context.resume_id)

        self.stage_tracker.link_document_id(context.task_id, context.resume_id)

        logger.warning("=== ResumeProcessingPipeline.run() RETURNING === resume_id=%s", context.resume_id)
        return context.resume_id

    def _run_text_extraction(self, context: ResumeProcessingContext) -> None:
        file_content = self.storage_service.download_file(
            bucket_name=self.RESUME_STORAGE_BUCKET,
            file_path=context.file_path,
        )
        context.raw_text = ResumeTextExtractionService.extract(file_content, context.source_format)
        if context.source_format == ResumeSourceFormat.PDF:
            context.page_count = TextExtractionService.get_pdf_page_count(file_content)

    def _run_text_cleaning(self, context: ResumeProcessingContext) -> None:
        context.cleaned_text = self.preprocessing_service.normalize(context.raw_text)

    def _run_pii_detection(self, context: ResumeProcessingContext) -> None:
        context.pii_findings = self.pii_detection_service.detect(context.cleaned_text)

    def _run_pii_redaction(self, context: ResumeProcessingContext) -> None:
        context.redacted_text = self.pii_redaction_service.redact(context.cleaned_text, context.pii_findings)

    def _run_ai_extraction(self, context: ResumeProcessingContext) -> None:
        prompt_template = self.prompt_template_repository.get_by_id(context.prompt_template_id)
        if prompt_template is None:
            raise ValueError(f"Prompt template '{context.prompt_template_id}' no longer exists.")
        context.raw_extraction = self.extraction_service.extract_raw(
            context.redacted_text,
            prompt=prompt_template.template_text,
            response_schema=ResumeExtractionGenerationSchema,
        )

    def _run_json_validation(self, context: ResumeProcessingContext) -> None:
        context.validated_extraction = ResumeExtractionResponse.model_validate(context.raw_extraction)

    def _run_skill_normalization(self, context: ResumeProcessingContext) -> None:
        context.skill_match_results = self.skill_normalization_service.normalize_skills(
            required_skills=context.validated_extraction.skills, preferred_skills=[],
        )

    def _run_persistence(self, context: ResumeProcessingContext) -> None:
        resume = self.resume_repository.get_by_id(context.resume_id)
        if resume is None:
            raise ValueError(f"Resume with ID {context.resume_id} not found.")

        self.resume_service.persist_processed_resume(
            resume=resume,
            extraction=context.validated_extraction,
            skill_repository=self.skill_repository,
            skill_matches=context.skill_match_results,
            attempt_number=context.attempt_number,
            page_count=context.page_count,
        )
