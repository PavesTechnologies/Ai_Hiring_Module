from uuid import UUID

from app.core.storage_service import StorageService
from app.models.async_tasks import ProcessingStage
from app.models.candidates import FileFormat, ParseAttemptStatus, ParseStatus
from app.repositories.resume_repository import ResumeRepository
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.extractions.gemini_resume_extraction_service import GeminiResumeExtractionService
from app.services.resume.resume_processing_context import ResumeProcessingContext

PARSER_NAME = "gemini-resume-parser"
PARSER_VERSION = "v1"

_IMAGE_FORMATS = (FileFormat.PNG, FileFormat.JPEG)
_STAGES_BEFORE_PERSISTENCE = (
    ProcessingStage.TEXT_EXTRACTION,
    ProcessingStage.TEXT_CLEANING,
    ProcessingStage.AI_EXTRACTION,
    ProcessingStage.JSON_VALIDATION,
)


class ResumeProcessingPipeline:
    """
    Text Extraction -> Text Cleaning -> AI Extraction -> JSON Validation ->
    Persistence. Deliberately stops there — no Skill Normalization /
    Embedding Generation stage, since resume-side skill extraction and
    embedding orchestration don't exist yet (a later epic slice; see the
    architecture mapping's "modules dependent on future work"). Validation
    and Storage already ran synchronously in ResumeIntakeService before
    this pipeline is invoked.
    """

    RESUME_STORAGE_BUCKET = "airs_resumes"

    def __init__(
        self,
        *,
        preprocessing_service: PreprocessingService,
        extraction_service: GeminiResumeExtractionService,
        storage_service: StorageService,
        resume_repository: ResumeRepository,
        stage_tracker: StageExecutionService,
    ):
        self.preprocessing_service = preprocessing_service
        self.extraction_service = extraction_service
        self.storage_service = storage_service
        self.resume_repository = resume_repository
        self.stage_tracker = stage_tracker

    def run(
        self,
        *,
        task_id: str,
        resume_id: UUID,
        candidate_id: UUID,
        file_path: str,
        file_format: FileFormat,
    ) -> None:
        context = ResumeProcessingContext(
            task_id=task_id,
            resume_id=resume_id,
            candidate_id=candidate_id,
            file_path=file_path,
            file_format=file_format,
        )

        if context.file_format in _IMAGE_FORMATS:
            self._handle_ocr_unsupported(context)
            return

        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.TEXT_EXTRACTION,
            lambda: self._run_text_extraction(context),
        )
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
            context.task_id, context.document_type, ProcessingStage.PERSISTENCE,
            lambda: self._run_persistence(context),
        )

        self.stage_tracker.link_document_id(context.task_id, context.resume_id)

    def _run_text_extraction(self, context: ResumeProcessingContext) -> None:
        file_content = self.storage_service.download_file(
            bucket_name=self.RESUME_STORAGE_BUCKET,
            file_path=context.file_path,
        )
        context.text = TextExtractionService.extract_for_resume(file_content, context.file_format)
        if context.file_format == FileFormat.PDF:
            context.page_count = TextExtractionService.get_pdf_page_count(file_content)

    def _run_text_cleaning(self, context: ResumeProcessingContext) -> None:
        context.cleaned_text = self.preprocessing_service.normalize(context.text)

    def _run_ai_extraction(self, context: ResumeProcessingContext) -> None:
        context.raw_extraction = self.extraction_service.extract_raw(context.cleaned_text)

    def _run_json_validation(self, context: ResumeProcessingContext) -> None:
        validated = ResumeExtractionResponse.model_validate(context.raw_extraction)
        context.raw_extraction = validated.model_dump()
        # Gemini doesn't return a confidence signal today — recorded as
        # fully-confident on a structurally-valid parse rather than left
        # null, so downstream consumers have a consistent field to read.
        context.parse_confidence_score = 1.0

    def _run_persistence(self, context: ResumeProcessingContext) -> None:
        resume = self.resume_repository.get_by_id(context.resume_id)
        resume.parsed_json = context.raw_extraction
        resume.parse_status = ParseStatus.PARSED
        resume.parser_version = f"{PARSER_NAME}-{PARSER_VERSION}"
        resume.page_count = context.page_count
        resume.ocr_used = False

        self.resume_repository.record_parse_attempt(
            resume_id=context.resume_id,
            attempt_number=1,
            parser_used=PARSER_NAME,
            parser_version=PARSER_VERSION,
            status=ParseAttemptStatus.SUCCESS,
            confidence_score=context.parse_confidence_score,
        )
        self.resume_repository.commit()

    def _handle_ocr_unsupported(self, context: ResumeProcessingContext) -> None:
        for stage in _STAGES_BEFORE_PERSISTENCE:
            self.stage_tracker.skip_stage(context.task_id, context.document_type, stage)

        self.stage_tracker.run_stage(
            context.task_id, context.document_type, ProcessingStage.PERSISTENCE,
            lambda: self._persist_ocr_unsupported(context),
        )
        self.stage_tracker.link_document_id(context.task_id, context.resume_id)

    def _persist_ocr_unsupported(self, context: ResumeProcessingContext) -> None:
        resume = self.resume_repository.get_by_id(context.resume_id)
        resume.parse_status = ParseStatus.FAILED

        self.resume_repository.record_parse_attempt(
            resume_id=context.resume_id,
            attempt_number=1,
            parser_used=PARSER_NAME,
            status=ParseAttemptStatus.FAILED,
            error_code="OCR_NOT_SUPPORTED",
            error_detail=(
                "Image-format resumes (PNG/JPEG) require OCR, which is not "
                "yet implemented."
            ),
        )
        self.resume_repository.commit()
