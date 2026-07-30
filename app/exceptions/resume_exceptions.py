from typing import Any


class ResumeException(Exception):
    """
    Base exception for resume-upload failures that need a specific,
    distinguishable HTTP response rather than a generic 500 — mirrors
    CampaignException's shape (message + status_code + optional data).
    One handler is registered for this base class; it catches every
    subclass below via FastAPI's exception-MRO lookup.
    """

    def __init__(self, message: str, status_code: int = 400, data: Any | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(self.message)


class UnsupportedFileFormatException(ResumeException):
    """Raised when the file's actual (magic-byte) content is not one of
    PDF/DOCX/PNG/JPEG, or doesn't match its claimed extension."""

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=400, data=data)


class FileSizeExceededException(ResumeException):
    """Raised when the file exceeds the configured RESUME_MAX_SIZE_MB limit."""

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=400, data=data)


class CorruptFileException(ResumeException):
    """Raised when the file is unreadable, truncated, or password-protected."""

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=400, data=data)


class EncryptionUnavailableException(ResumeException):
    """Raised when no ACTIVE or ROTATING encryption key exists for a purpose."""

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=503, data=data)


class DuplicateResumeFileException(ResumeException):
    """
    Epic 3 (M05-E03) Phase C2 — raised when an uploaded file's hash matches
    a resume already in the system and no resolution ("use_existing" /
    "upload_anyway") was supplied. `data` carries a DuplicateFileWarningResponse.
    """

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=409, data=data)


class ResumeNotRetryableException(ResumeException):
    """Epic 4 (M05-E04) Phase D10 - raised when retry_parse is called on a resume not currently parse_status=FAILED."""

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=409, data=data)


class DeadLetterEntryNotReplayableException(ResumeException):
    """
    Epic 4 (M05-E04) Phase D10 - raised when replay_from_dlq is called on
    a DLQ entry that's already been replayed, or isn't resume-scoped
    (resume_id is None - e.g. a JD document's dead-lettered failure).
    """

    def __init__(self, message: str, data: Any | None = None):
        super().__init__(message, status_code=409, data=data)
