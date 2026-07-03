class StorageException(Exception):
    """Raised when a storage operation fails."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)