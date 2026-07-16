import io
from dataclasses import dataclass
from zipfile import BadZipFile

import filetype
import pypdfium2 as pdfium
from docx import Document as DocxDocument
from PIL import Image, UnidentifiedImageError

from app.exceptions.resume_exceptions import (
    CorruptFileException,
    FileSizeExceededException,
    UnsupportedFileFormatException,
)
from app.models.candidates import FileFormat
from app.repositories.config_repository import ConfigRepository

DEFAULT_MAX_SIZE_MB = 10.0

_EXTENSION_TO_FORMAT = {
    "pdf": FileFormat.PDF,
    "docx": FileFormat.DOCX,
    "png": FileFormat.PNG,
    "jpg": FileFormat.JPEG,
    "jpeg": FileFormat.JPEG,
}


@dataclass
class FileValidationResult:
    file_format: FileFormat
    size_bytes: int


class FileValidationService:
    def __init__(self, config_repo: ConfigRepository):
        self.config_repo = config_repo

    def validate(self, file_bytes: bytes, filename: str) -> FileValidationResult:
        """
        Runs format detection, size check, and integrity check, in that
        order — matching the sequence the epic specifies so the cheapest
        checks reject bad input before the more expensive open-attempt.
        Raises one of UnsupportedFileFormatException / FileSizeExceededException /
        CorruptFileException with a specific, user-facing reason on failure.
        """
        detected_format = self._detect_format(file_bytes, filename)
        self._check_size(file_bytes)
        self._check_integrity(file_bytes, detected_format)
        return FileValidationResult(file_format=detected_format, size_bytes=len(file_bytes))

    def _detect_format(self, file_bytes: bytes, filename: str) -> FileFormat:
        kind = filetype.guess(file_bytes)
        if kind is None or kind.extension.lower() not in _EXTENSION_TO_FORMAT:
            detected_label = kind.extension if kind else "unrecognized"
            raise UnsupportedFileFormatException(
                "Unsupported file format — please upload PDF, DOCX, PNG, or JPEG files only. "
                f"Detected: {detected_label}."
            )

        detected_format = _EXTENSION_TO_FORMAT[kind.extension.lower()]

        claimed_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        claimed_format = _EXTENSION_TO_FORMAT.get(claimed_ext)
        if claimed_format is not None and claimed_format != detected_format:
            raise UnsupportedFileFormatException(
                f"File content does not match its extension — '{filename}' claims "
                f".{claimed_ext} but its actual content is {detected_format.value}. "
                "Please upload a genuine PDF, DOCX, PNG, or JPEG file."
            )

        return detected_format

    def _check_size(self, file_bytes: bytes) -> None:
        max_mb = self._max_size_mb()
        max_bytes = max_mb * 1024 * 1024
        if len(file_bytes) > max_bytes:
            actual_mb = len(file_bytes) / (1024 * 1024)
            raise FileSizeExceededException(
                f"File size {actual_mb:.1f} MB exceeds the {max_mb:g} MB limit."
            )

    def _max_size_mb(self) -> float:
        configs = self.config_repo.get_configs_by_keys(["RESUME_MAX_SIZE_MB"])
        raw = configs.get("RESUME_MAX_SIZE_MB")
        if not raw:
            return DEFAULT_MAX_SIZE_MB
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_MAX_SIZE_MB

    def _check_integrity(self, file_bytes: bytes, file_format: FileFormat) -> None:
        if file_format == FileFormat.PDF:
            self._check_pdf(file_bytes)
        elif file_format == FileFormat.DOCX:
            self._check_docx(file_bytes)
        else:
            self._check_image(file_bytes)

    @staticmethod
    def _check_pdf(file_bytes: bytes) -> None:
        try:
            pdfium.PdfDocument(file_bytes)
        except pdfium.PdfiumError as exc:
            if "password" in str(exc).lower():
                raise CorruptFileException(
                    "PDF is password protected — please upload an unprotected version."
                ) from exc
            raise CorruptFileException(f"PDF file is corrupted or unreadable: {exc}") from exc

    @staticmethod
    def _check_docx(file_bytes: bytes) -> None:
        try:
            DocxDocument(io.BytesIO(file_bytes))
        except BadZipFile as exc:
            raise CorruptFileException("DOCX file is corrupted or unreadable.") from exc
        except Exception as exc:
            raise CorruptFileException(f"DOCX file is corrupted or unreadable: {exc}") from exc

    @staticmethod
    def _check_image(file_bytes: bytes) -> None:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            image.verify()
        except UnidentifiedImageError as exc:
            raise CorruptFileException("Image file is corrupted or unreadable.") from exc
        except Exception as exc:
            raise CorruptFileException(f"Image file is corrupted or unreadable: {exc}") from exc
