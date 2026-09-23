from app.services.extractions.llm_extraction_service import LLMExtractionService
from app.services.llm.base import LLMProvider
from app.services.llm.factory import build_env_fallback_provider


class GeminiExtractionService(LLMExtractionService):
    """
    Backward-compatible name for LLMExtractionService. With no provider it
    behaves exactly as before (Gemini configured from .env); the Celery
    tasks now pass resolve_active_provider(db) so the admin's saved
    Settings config wins.
    """

    def __init__(self, provider: LLMProvider | None = None):
        super().__init__(provider or build_env_fallback_provider())
