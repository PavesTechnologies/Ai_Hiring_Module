from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import ProcessingStage
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_redaction_service import PIIRedactionService
from app.services.resume.resume_processing_context import ResumeProcessingContext
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline

MODULE = "app.services.resume.resume_processing_pipeline"

EXPECTED_FULL_STAGE_ORDER = [
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

RAW_TEXT_WITH_PII = "John Doe. Email: john@gmail.com. Phone: +91 9876543210."


class RecordingStageTracker:
    """
    Stands in for StageExecutionService: actually invokes each stage's fn
    (mirroring StageExecutionService.run_stage's real behavior) so the
    pipeline genuinely runs end-to-end, while recording call order for
    assertions.
    """

    def __init__(self):
        self.calls: list[ProcessingStage] = []

    def run_stage(self, task_id, document_type, stage, fn, attempt_number=1, context=None, checkpoint_repo=None):
        self.calls.append(stage)
        return fn()

    def link_document_id(self, task_id, document_id):
        pass


def _build_pipeline(stage_tracker, extraction_service, skill_normalization_service, resume_repository, resume_service):
    return ResumeProcessingPipeline(
        preprocessing_service=MagicMock(normalize=MagicMock(return_value=RAW_TEXT_WITH_PII.lower())),
        extraction_service=extraction_service,
        hash_service=MagicMock(generate_hash=MagicMock(return_value="hash123")),
        storage_service=MagicMock(download_file=MagicMock(return_value=b"raw bytes")),
        skill_normalization_service=skill_normalization_service,
        embedding_service=MagicMock(generate_embedding=MagicMock(return_value=[0.1, 0.2, 0.3])),
        resume_service=resume_service,
        resume_repository=resume_repository,
        skill_repository=MagicMock(),
        stage_tracker=stage_tracker,
        pii_detection_service=PIIDetectionService(),
        pii_redaction_service=PIIRedactionService(),
    )


def _make_extraction_response_dict(full_name="John Doe"):
    return {
        "full_name": full_name,
        "skills": ["Python"],
        "work_experience": [],
        "education": [],
        "certifications": [],
        "total_experience_years": None,
        "summary": None,
        "metadata": {},
    }


def test_single_upload_flow_runs_all_nine_stages_in_order_and_redacts_before_ai():
    stage_tracker = RecordingStageTracker()
    extraction_service = MagicMock()
    extraction_service.extract_raw.return_value = _make_extraction_response_dict()
    resume_repository = MagicMock()
    resume_repository.get_active_embedding_model_version.return_value = MagicMock(id=uuid4())
    resume_repository.get_by_id.return_value = MagicMock()
    resume_service = MagicMock()

    pipeline = _build_pipeline(
        stage_tracker, extraction_service,
        skill_normalization_service=MagicMock(normalize_skills=MagicMock(return_value=[])),
        resume_repository=resume_repository, resume_service=resume_service,
    )

    with patch(f"{MODULE}.ResumeTextExtractionService.extract", return_value=RAW_TEXT_WITH_PII):
        resume_id = uuid4()
        result = pipeline.run(
            task_id="task-1",
            resume_id=resume_id,
            candidate_id=uuid4(),
            file_path="some/path.pdf",
            source_format=ResumeSourceFormat.PDF,
        )

    assert result == resume_id
    assert stage_tracker.calls == EXPECTED_FULL_STAGE_ORDER

    # AI extraction must have been called with redacted text, never the raw/
    # cleaned text -- this is the actual behavioral guarantee of the whole
    # feature, verified end-to-end through the real pipeline object rather
    # than by reading the source.
    called_text = extraction_service.extract_raw.call_args[0][0]
    assert "john@gmail.com" not in called_text
    assert "9876543210" not in called_text
    assert "[EMAIL]" in called_text
    assert "[PHONE]" in called_text

    resume_service.persist_processed_resume.assert_called_once()
    persisted_extraction = resume_service.persist_processed_resume.call_args.kwargs["extraction"]
    assert isinstance(persisted_extraction, ResumeExtractionResponse)
    assert persisted_extraction.full_name == "John Doe"


def test_bulk_upload_style_initial_context_skips_already_populated_stages():
    """
    Mirrors how bulk_upload_tasks.py calls pipeline.run(initial_context=...)
    after running TEXT_EXTRACTION..JSON_VALIDATION itself -- confirms the
    skip-if-already-populated check still works correctly now that there are
    six populated fields (not four) ahead of SKILL_NORMALIZATION.
    """
    stage_tracker = RecordingStageTracker()
    extraction_service = MagicMock()  # must NOT be called again
    resume_repository = MagicMock()
    resume_repository.get_active_embedding_model_version.return_value = MagicMock(id=uuid4())
    resume_repository.get_by_id.return_value = MagicMock()
    resume_service = MagicMock()

    pipeline = _build_pipeline(
        stage_tracker, extraction_service,
        skill_normalization_service=MagicMock(normalize_skills=MagicMock(return_value=[])),
        resume_repository=resume_repository, resume_service=resume_service,
    )

    findings = PIIDetectionService().detect(RAW_TEXT_WITH_PII.lower())
    redacted = PIIRedactionService().redact(RAW_TEXT_WITH_PII.lower(), findings)

    initial_context = ResumeProcessingContext(
        task_id="task-2",
        file_path="some/path.pdf",
        source_format=ResumeSourceFormat.PDF,
        raw_text=RAW_TEXT_WITH_PII,
        cleaned_text=RAW_TEXT_WITH_PII.lower(),
        pii_findings=findings,
        redacted_text=redacted,
        raw_extraction=_make_extraction_response_dict(),
        validated_extraction=ResumeExtractionResponse.model_validate(_make_extraction_response_dict()),
    )

    resume_id = uuid4()
    pipeline.run(
        task_id="task-2",
        resume_id=resume_id,
        candidate_id=uuid4(),
        file_path="some/path.pdf",
        source_format=ResumeSourceFormat.PDF,
        initial_context=initial_context,
    )

    assert stage_tracker.calls == [
        ProcessingStage.SKILL_NORMALIZATION,
        ProcessingStage.EMBEDDING_GENERATION,
        ProcessingStage.PERSISTENCE,
    ]
    extraction_service.extract_raw.assert_not_called()
