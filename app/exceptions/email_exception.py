class EmailDeliveryException(Exception):
    """Raised when an SES send_email call fails. Wraps the original exception for retry classification."""

    def __init__(self, message: str, original: Exception):
        self.message = message
        self.original = original
        super().__init__(message)
