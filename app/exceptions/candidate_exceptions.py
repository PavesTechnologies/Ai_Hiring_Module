class CandidateErasureBlockedException(Exception):
    """
    Raised when an upload targets a candidate (matched by email_hash) who has
    an active or completed erasure request — the platform must not re-ingest
    data for a candidate who has exercised their right to be forgotten.
    """

    def __init__(self, message: str = "This candidate has an active data erasure request — their data cannot be re-submitted to the platform."):
        self.message = message
        super().__init__(message)
