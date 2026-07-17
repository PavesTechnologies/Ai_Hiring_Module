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


class MaxFilesExceededException(ResumeException):
    """Raised when a ZIP archive contains more real files than MAX_FILES_PER_ZIP."""

    def __init__(self, message: str, data=None):
        super().__init__(message, status_code=400, data=data)


class BulkUploadJobNotFoundException(ResumeException):
    """Raised when a bulk_upload_jobs row doesn't exist for the given id."""

    def __init__(self, message: str, data=None):
        super().__init__(message, status_code=404, data=data)


class BulkUploadJobNotCancellableException(ResumeException):
    """Raised when cancellation is attempted on a job that has already reached a terminal state."""

    def __init__(self, message: str, data=None):
        super().__init__(message, status_code=409, data=data)
