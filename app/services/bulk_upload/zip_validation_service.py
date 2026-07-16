import filetype

from app.exceptions.bulk_upload_exceptions import (
    UnsupportedArchiveFormatException,
    ZipSizeExceededException,
)
from app.repositories.config_repository import ConfigRepository

DEFAULT_ZIP_MAX_SIZE_MB = 500.0

_ZIP_MIME_TYPES = {"application/zip", "application/x-zip-compressed"}


class ZipValidationService:
    """
    Validates the uploaded bulk-upload archive itself — extension, magic-byte
    format, and size. Deliberately does NOT attempt to open the archive
    (corruption/password-protection can only be detected that way) — that's
    S02-T01's job, run asynchronously during extraction, not here.
    """

    def __init__(self, config_repo: ConfigRepository):
        self.config_repo = config_repo

    def validate(self, file_bytes: bytes, filename: str) -> None:
        self._check_extension(filename)
        self._check_format(file_bytes)
        self._check_size(file_bytes)

    @staticmethod
    def _check_extension(filename: str) -> None:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext != "zip":
            raise UnsupportedArchiveFormatException(
                "Only ZIP archives are accepted for bulk upload — "
                f"'{filename}' has extension '.{ext or 'none'}'."
            )

    @staticmethod
    def _check_format(file_bytes: bytes) -> None:
        kind = filetype.guess(file_bytes)
        detected_mime = kind.mime if kind else None
        if detected_mime not in _ZIP_MIME_TYPES:
            detected_label = kind.extension if kind else "unrecognized"
            raise UnsupportedArchiveFormatException(
                "Unsupported archive format detected — please upload a "
                f"genuine ZIP file. Detected: {detected_label}."
            )

    def _check_size(self, file_bytes: bytes) -> None:
        max_mb = self._max_size_mb()
        max_bytes = max_mb * 1024 * 1024
        if len(file_bytes) > max_bytes:
            actual_mb = len(file_bytes) / (1024 * 1024)
            raise ZipSizeExceededException(
                f"ZIP file size {actual_mb:.1f} MB exceeds the {max_mb:g} MB limit."
            )

    def _max_size_mb(self) -> float:
        configs = self.config_repo.get_configs_by_keys(["ZIP_MAX_SIZE_MB"])
        raw = configs.get("ZIP_MAX_SIZE_MB")
        if not raw:
            return DEFAULT_ZIP_MAX_SIZE_MB
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_ZIP_MAX_SIZE_MB
