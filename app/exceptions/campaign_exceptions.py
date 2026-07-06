from typing import T


class CampaignException(Exception):
    """Base exception for all campaign-related errors."""
    def __init__(self, message: str, status_code: int = 400, data: T | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(self.message)