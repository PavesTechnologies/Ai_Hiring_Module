import io

import pypdfium2 as pdfium
from docx import Document

from app.models.candidates import FileFormat
from app.models.jd.job_descriptions import JDSourceFormat


class TextExtractionService:
    """
    Extracts raw text from an uploaded document. Document-type-agnostic —
    the pdfium/python-docx logic has no JD-specific content, so it lives
    under the shared document_processing package (alongside
    StageExecutionService) rather than under app/services/jd/, ready for a
    future Resume pipeline to reuse without relocating it again.
    """

    @staticmethod
    def extract_pdf_text(file_content: bytes) -> str:
        pdf = pdfium.PdfDocument(file_content)
        pages_text = [page.get_textpage().get_text_range() for page in pdf]
        return "\n".join(pages_text)

    @staticmethod
    def extract_docx_text(file_content: bytes) -> str:
        document = Document(io.BytesIO(file_content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    @classmethod
    def extract(cls, file_content: bytes, source_format: JDSourceFormat) -> str:
        if source_format == JDSourceFormat.PDF:
            return cls.extract_pdf_text(file_content)
        return cls.extract_docx_text(file_content)

    @classmethod
    def extract_for_resume(cls, file_content: bytes, file_format: FileFormat) -> str:
        """
        FileFormat-dispatched counterpart to extract() for the resume
        pipeline, which validates PDF/DOCX/PNG/JPEG (FileFormat) rather
        than JD's TEXT/PDF/DOCX (JDSourceFormat). PNG/JPEG require OCR,
        which isn't implemented yet — callers should route image-format
        resumes around this method entirely (see
        ResumeProcessingPipeline._mark_ocr_unsupported); this raises rather
        than silently returning empty text if reached directly.
        """
        if file_format == FileFormat.PDF:
            return cls.extract_pdf_text(file_content)
        if file_format == FileFormat.DOCX:
            return cls.extract_docx_text(file_content)
        raise ValueError(
            f"Text extraction for {file_format.value} resumes requires OCR, "
            "which is not yet implemented."
        )

    @staticmethod
    def get_pdf_page_count(file_content: bytes) -> int:
        pdf = pdfium.PdfDocument(file_content)
        return len(pdf)
