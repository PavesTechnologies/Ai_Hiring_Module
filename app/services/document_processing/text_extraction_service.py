import io

import pypdfium2 as pdfium
from docx import Document

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
