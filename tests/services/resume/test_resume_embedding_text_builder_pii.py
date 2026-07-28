from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse, WorkExperience
from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.resume.resume_embedding_text_builder import build_canonical_embedding_text


def test_full_name_never_included_in_embedding_text():
    extraction = ResumeExtractionResponse(
        full_name="Jane Doe",
        skills=["Python", "FastAPI"],
        summary="Backend engineer with 5 years of experience.",
    )

    embedding_text = build_canonical_embedding_text(extraction)

    assert "Jane Doe" not in embedding_text
    assert "Jane" not in embedding_text
    assert "Doe" not in embedding_text


def test_embedding_text_built_from_ai_output_carries_no_target_pii_types():
    """
    AI_EXTRACTION now runs against context.redacted_text (see
    ResumeProcessingPipeline._run_ai_extraction) -- Gemini physically cannot
    return an email/phone/LinkedIn/GitHub/portfolio URL it never received, so
    summary/work_experience.description in a well-behaved response can't
    carry them either. This asserts that guarantee holds for a realistic
    extraction whose free-text fields echo redaction placeholders (the
    expected, correct AI behavior per RESUME_SYSTEM_PROMPT's instruction not
    to invent contact info), and that scanning the resulting embedding text
    with the same deterministic detector turns up nothing.

    Note this is NOT a second redaction pass -- build_canonical_embedding_text
    does not itself scrub free text. The guarantee lives entirely upstream,
    at the point contact info is stripped before AI_EXTRACTION ever runs.
    """
    extraction = ResumeExtractionResponse(
        full_name="Jane Doe",
        skills=["Python", "FastAPI"],
        work_experience=[
            WorkExperience(
                title="Backend Engineer",
                company="Acme Corp",
                description="Reachable via [EMAIL] during business hours.",
            ),
        ],
        summary="Contact preferred over [EMAIL] or [PHONE]; portfolio at [PORTFOLIO].",
    )

    embedding_text = build_canonical_embedding_text(extraction)
    findings = PIIDetectionService().detect(embedding_text)

    assert findings == []
