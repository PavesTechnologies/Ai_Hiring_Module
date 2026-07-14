from uuid import UUID

from app.models.async_tasks import DocumentType, ProcessingStage
from app.models.jd.job_descriptions import JDSourceFormat
from app.repositories.jd_repository import JDRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.services.ai.embedding_service import EmbeddingService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
from app.services.jd.jd_processing_context import JDProcessingContext
from app.services.jd.jd_service import JDService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService


class JDProcessingPipeline:
    """
    Orchestrates the finalized async JD document-processing pipeline:
    Text Extraction -> Text Cleaning -> AI Extraction -> JSON Validation ->
    Skill Normalization -> Embedding Generation -> Persistence. Validation
    and Storage already ran synchronously in the route before this pipeline
    is invoked (see app/api/routes/jd_routes.py).

    Each stage is a private method of the shape fn(context) -> None: it
    reads what it needs from a JDProcessingContext and writes its result
    back onto it, rather than closing over ad hoc local variables. This is
    also what makes the pipeline retry-ready — a future retry driver can
    re-invoke a single failed stage against the same context without
    recomputing everything that already succeeded before it.

    Concrete and JD-specific by design: when a Resume pipeline is built, it
    defines its own ResumeProcessingContext and reuses StageExecutionService
    directly, rather than sharing a base class guessed ahead of a second
    real caller.
    """

    def __init__(
        self,
        *,
        preprocessing_service: PreprocessingService,
        extraction_service: GeminiExtractionService,
        hash_service: HashService,
        storage_service: StorageService,
        skill_normalization_service: SkillNormalizationService,
        embedding_service: EmbeddingService,
        jd_service: JDService,
        jd_repository: JDRepository,
        skill_repository: SkillRepository,
        stage_tracker: StageExecutionService,
    ):
        self.preprocessing_service = preprocessing_service
        self.extraction_service = extraction_service
        self.hash_service = hash_service
        self.storage_service = storage_service
        self.skill_normalization_service = skill_normalization_service
        self.embedding_service = embedding_service
        self.jd_service = jd_service
        self.jd_repository = jd_repository
        self.skill_repository = skill_repository
        self.stage_tracker = stage_tracker

    def run(
        self,
        *,
        task_id: str,
        raw_text: str | None,
        file_path: str | None,
        title: str,
        jurisdiction: str,
        min_experience_years: float | None,
        education_criteria: dict | None,
        created_by: str,
        existing_jd_id: UUID | None = None,
        version_number: int = 1,
        parent_jd_id: UUID | None = None,
        lineage_root_id: UUID | None = None,
    ) -> UUID | None:
        context = JDProcessingContext(
            task_id=task_id,
            title=title,
            jurisdiction=jurisdiction,
            min_experience_years=min_experience_years,
            education_criteria=education_criteria,
            created_by=created_by,
            file_path=file_path,
            raw_text=raw_text,
            existing_jd_id=existing_jd_id,
            version_number=version_number,
            parent_jd_id=parent_jd_id,
            lineage_root_id=lineage_root_id,
        )
        context.source_format = self._resolve_source_format(file_path)
        is_reprocess = existing_jd_id is not None

        if context.raw_text is not None:
            # JSON-body submissions already passed a synchronous duplicate
            # pre-check in the route before this task was even queued.
            self.stage_tracker.skip_stage(context.task_id, context.document_type, ProcessingStage.TEXT_EXTRACTION)
            context.text = context.raw_text
        else:
            self.stage_tracker.run_stage(
                context.task_id, context.document_type, ProcessingStage.TEXT_EXTRACTION,
                lambda: self._run_text_extraction(context),
            )

            context.content_hash = self.hash_service.generate_hash(context.text)
            duplicate = (
                self.jd_repository.get_duplicate_excluding_lineage(
                    content_hash=context.content_hash, lineage_root_id=lineage_root_id,
                )
                if is_reprocess
                else self.jd_repository.get_by_content_hash(context.content_hash)
            )
            if duplicate:
                self._skip_remaining_after_text_extraction(context)
                return None

        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.TEXT_CLEANING,
            lambda: self._run_text_cleaning(context),
        )
        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.AI_EXTRACTION,
            lambda: self._run_ai_extraction(context),
        )
        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.JSON_VALIDATION,
            lambda: self._run_json_validation(context),
        )
        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.SKILL_NORMALIZATION,
            lambda: self._run_skill_normalization(context),
        )
        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.EMBEDDING_GENERATION,
            lambda: self._run_embedding_generation(context),
        )
        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.PERSISTENCE,
            lambda: self._run_persistence(context),
        )

        if context.jd_id:
            self.stage_tracker.link_document_id(context.task_id, context.jd_id)

        return context.jd_id

    def _run_text_extraction(self, context: JDProcessingContext) -> None:
        file_content = self.storage_service.download_file(
            bucket_name=self.jd_service.JD_STORAGE_BUCKET,
            file_path=context.file_path,
        )
        context.text = TextExtractionService.extract(file_content, context.source_format)

    def _run_text_cleaning(self, context: JDProcessingContext) -> None:
        context.cleaned_text = self.preprocessing_service.normalize(context.text)

    def _run_ai_extraction(self, context: JDProcessingContext) -> None:
        context.raw_extraction = self.extraction_service.extract_raw(context.cleaned_text)

    def _run_json_validation(self, context: JDProcessingContext) -> None:
        context.extraction = JDExtractionResponse.model_validate(context.raw_extraction)

    def _run_skill_normalization(self, context: JDProcessingContext) -> None:
        context.skill_matches = self.skill_normalization_service.normalize_skills(
            context.extraction.required_skills, context.extraction.preferred_skills,
        )

    def _run_embedding_generation(self, context: JDProcessingContext) -> None:
        context.embedding_text = self.embedding_service.build_canonical_embedding_text(
            context.extraction, context.title,
        )
        context.embedding = self.embedding_service.generate_embedding(context.embedding_text)
        context.input_text_hash = self.hash_service.generate_hash(context.embedding_text)

    def _run_persistence(self, context: JDProcessingContext) -> None:
        embedding_model_version = self.jd_repository.get_active_embedding_model_version()
        context.embedding_model_version_id = embedding_model_version.id

        if context.content_hash is None:
            context.content_hash = self.hash_service.generate_hash(context.text)

        context.jd_id = self.jd_service.persist_processed_jd(
            title=context.title,
            raw_text=context.text,
            jurisdiction=context.jurisdiction,
            min_experience_years=context.min_experience_years,
            education_criteria=context.education_criteria,
            source_format=context.source_format,
            file_path=context.file_path,
            created_by=context.created_by,
            content_hash=context.content_hash,
            extraction=context.extraction,
            skill_repository=self.skill_repository,
            skill_matches=context.skill_matches,
            embedding=context.embedding,
            embedding_model_version_id=context.embedding_model_version_id,
            input_text_hash=context.input_text_hash,
            existing_jd_id=context.existing_jd_id,
            version_number=context.version_number,
            parent_jd_id=context.parent_jd_id,
            lineage_root_id=context.lineage_root_id,
        )

    def _skip_remaining_after_text_extraction(self, context: JDProcessingContext) -> None:
        for stage in (
            ProcessingStage.TEXT_CLEANING,
            ProcessingStage.AI_EXTRACTION,
            ProcessingStage.JSON_VALIDATION,
            ProcessingStage.SKILL_NORMALIZATION,
            ProcessingStage.EMBEDDING_GENERATION,
            ProcessingStage.PERSISTENCE,
        ):
            self.stage_tracker.skip_stage(context.task_id, context.document_type, stage)

    @staticmethod
    def _resolve_source_format(file_path: str | None) -> JDSourceFormat:
        if not file_path:
            return JDSourceFormat.TEXT
        extension = file_path.rsplit(".", 1)[-1].lower()
        return JDSourceFormat.PDF if extension == "pdf" else JDSourceFormat.DOCX
