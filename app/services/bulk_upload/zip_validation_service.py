import filetype

from app.exceptions.bulk_upload_exceptions import (
    MaxFilesExceededException,
    UnsupportedArchiveFormatException,
    ZipSizeExceededException,
)
from app.repositories.config_repository import ConfigRepository

DEFAULT_ZIP_MAX_SIZE_MB = 500.0
DEFAULT_MAX_FILES_PER_ZIP = 200

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

    def validate_file_count(self, file_count: int) -> None:
        """
        Run once extraction has enumerated the archive's real entries
        (S02-T01 territory — the extraction task, not upload-time
        validation, since the count isn't known until the ZIP is opened).
        Rejects the whole job rather than silently processing a truncated
        subset of an oversized archive.
        """
        max_files = self._max_files_per_zip()
        if file_count > max_files:
            raise MaxFilesExceededException(
                f"ZIP archive contains {file_count} files, exceeding the "
                f"{max_files}-file limit per bulk upload."
            )

    def _max_files_per_zip(self) -> int:
        configs = self.config_repo.get_configs_by_keys(["MAX_FILES_PER_ZIP"])
        raw = configs.get("MAX_FILES_PER_ZIP")
        if not raw:
            return DEFAULT_MAX_FILES_PER_ZIP
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_MAX_FILES_PER_ZIP
