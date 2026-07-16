from app.exceptions.resume_exceptions import ResumeException


class UnsupportedArchiveFormatException(ResumeException):
    """
    Raised when the uploaded bulk-upload file is not a genuine ZIP archive
    (wrong extension, or magic bytes don't match application/zip /
    application/x-zip-compressed).
    """

    def __init__(self, message: str, data=None):
        super().__init__(message, status_code=400, data=data)


class ZipSizeExceededException(ResumeException):
    """Raised when the ZIP archive exceeds the configured ZIP_MAX_SIZE_MB limit."""

    def __init__(self, message: str, data=None):
        super().__init__(message, status_code=400, data=data)
