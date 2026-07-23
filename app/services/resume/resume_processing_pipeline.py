import logging
from uuid import UUID

from app.models.async_tasks import ProcessingStage
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.prompts.resume_extraction_prompt import RESUME_SYSTEM_PROMPT
from app.schemas.ai.resume_extraction_response import ResumeExtractionGenerationSchema, ResumeExtractionResponse
from app.services.ai.embedding_service import EmbeddingService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
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
    -> Embedding Generation -> Persistence. Mirrors JDProcessingPipeline's
    stage loop and StageExecutionService usage, with one deliberate
    deviation: stages are run WITHOUT `context=`/`checkpoint_repo=` args.

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
        hash_service: HashService,
        storage_service: StorageService,
        skill_normalization_service: SkillNormalizationService,
        embedding_service: EmbeddingService,
        resume_service: ResumeService,
        resume_repository: ResumeRepository,
        skill_repository: SkillRepository,
        stage_tracker: StageExecutionService,
    ):
        self.preprocessing_service = preprocessing_service
        self.extraction_service = extraction_service
        self.hash_service = hash_service
        self.storage_service = storage_service
        self.skill_normalization_service = skill_normalization_service
        self.embedding_service = embedding_service
        self.resume_service = resume_service
        self.resume_repository = resume_repository
        self.skill_repository = skill_repository
        self.stage_tracker = stage_tracker

    def run(
        self,
        *,
        task_id: str,
        resume_id: UUID,
        candidate_id: UUID,
        file_path: str,
        source_format: ResumeSourceFormat,
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
        context.attempt_number = attempt_number

        for stage, output_attr, fn in (
            (ProcessingStage.TEXT_EXTRACTION, "raw_text", lambda: self._run_text_extraction(context)),
            (ProcessingStage.TEXT_CLEANING, "cleaned_text", lambda: self._run_text_cleaning(context)),
            (ProcessingStage.AI_EXTRACTION, "raw_extraction", lambda: self._run_ai_extraction(context)),
            (ProcessingStage.JSON_VALIDATION, "validated_extraction", lambda: self._run_json_validation(context)),
            (ProcessingStage.SKILL_NORMALIZATION, "skill_match_results", lambda: self._run_skill_normalization(context)),
            (ProcessingStage.EMBEDDING_GENERATION, "embedding", lambda: self._run_embedding_generation(context)),
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

    def _run_text_cleaning(self, context: ResumeProcessingContext) -> None:
        context.cleaned_text = self.preprocessing_service.normalize(context.raw_text)

    def _run_ai_extraction(self, context: ResumeProcessingContext) -> None:
        context.raw_extraction = self.extraction_service.extract_raw(
            context.cleaned_text,
            prompt=RESUME_SYSTEM_PROMPT,
            response_schema=ResumeExtractionGenerationSchema,
        )

    def _run_json_validation(self, context: ResumeProcessingContext) -> None:
        context.validated_extraction = ResumeExtractionResponse.model_validate(context.raw_extraction)

    def _run_skill_normalization(self, context: ResumeProcessingContext) -> None:
        context.skill_match_results = self.skill_normalization_service.normalize_skills(
            required_skills=context.validated_extraction.skills, preferred_skills=[],
        )

    def _run_embedding_generation(self, context: ResumeProcessingContext) -> None:
        context.embedding_text = resume_embedding_text_builder.build_canonical_embedding_text(
            context.validated_extraction,
        )
        context.embedding = self.embedding_service.generate_embedding(context.embedding_text)
        context.input_text_hash = self.hash_service.generate_hash(context.embedding_text)

    def _run_persistence(self, context: ResumeProcessingContext) -> None:
        embedding_model_version = self.resume_repository.get_active_embedding_model_version()
        context.embedding_model_version_id = embedding_model_version.id

        resume = self.resume_repository.get_by_id(context.resume_id)
        if resume is None:
            raise ValueError(f"Resume with ID {context.resume_id} not found.")

        self.resume_service.persist_processed_resume(
            resume=resume,
            extraction=context.validated_extraction,
            skill_repository=self.skill_repository,
            skill_matches=context.skill_match_results,
            embedding=context.embedding,
            embedding_model_version_id=context.embedding_model_version_id,
            input_text_hash=context.input_text_hash,
            attempt_number=context.attempt_number,
        )
