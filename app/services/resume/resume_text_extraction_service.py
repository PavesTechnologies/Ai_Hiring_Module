from app.models.resume.resume_source_format import ResumeSourceFormat
from app.services.document_processing.text_extraction_service import TextExtractionService


class ResumeTextExtractionService:
    """
    Resume-facing counterpart to TextExtractionService.extract() — same
    pdfium/python-docx logic, reached via TextExtractionService's own static
    methods rather than duplicating them, but keyed on ResumeSourceFormat
    instead of JDSourceFormat so the Resume pipeline never has to import a
    JD-specific enum.
    """

    @classmethod
    def extract(cls, file_content: bytes, source_format: ResumeSourceFormat) -> str:
        if source_format == ResumeSourceFormat.PDF:
            return TextExtractionService.extract_pdf_text(file_content)
        return TextExtractionService.extract_docx_text(file_content)
