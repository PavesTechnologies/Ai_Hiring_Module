import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMProvider, LLMProviderName
from app.services.llm.gemini_provider import GeminiProvider
from app.services.llm.openai_provider import GroqProvider, OpenAIProvider

logger = logging.getLogger(__name__)

AI_PROVIDER_KEY_PURPOSE = "AI_PROVIDER_KEY"


def build_provider(
    provider: str,
    api_key: str,
    model: str,
    *,
    timeout_seconds: float | None = None,
) -> LLMProvider:
    """
    Builds a provider client from explicit values - used both for the saved
    config and for the Settings "Check" button's unsaved values.
    """
    timeout = timeout_seconds or settings.gemini_timeout_ms / 1000
    if provider == LLMProviderName.GOOGLE:
        return GeminiProvider(api_key=api_key, model=model, timeout_ms=int(timeout * 1000))
    if provider == LLMProviderName.ANTHROPIC:
        return AnthropicProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                                 max_retries=settings.llm_max_retries)
    if provider == LLMProviderName.OPENAI:
        return OpenAIProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                              max_retries=settings.llm_max_retries)
    if provider == LLMProviderName.GROQ:
        return GroqProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                            max_retries=settings.llm_max_retries)
    raise LLMPermanentError(f"Unsupported AI provider '{provider}'.", reason=LLMErrorReason.BAD_REQUEST)


def build_env_fallback_provider() -> LLMProvider:
    """The pre-existing behaviour: Gemini configured from .env."""
    return GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model)


def resolve_active_provider(db: Session) -> LLMProvider:
    """
    Called at the start of every AI-using Celery task, so a config saved
    in Settings applies to the next task without restarting workers. The
    decrypted key only ever lives in this process's memory - it is never
    cached in Redis.

    Falls back to .env Gemini when no verified config is saved, or when
    the saved key can't be decrypted (e.g. ENCRYPTION_KEY_AI_PROVIDER_V1
    missing on this worker) - logged loudly, since processing then runs
    on a provider the admin didn't pick.
    """
    config = AIProviderConfigRepository(db).get_active()
    if config is None or not config.is_verified:
        logger.info("LLM provider: .env fallback (provider=%s model=%s)",
                    LLMProviderName.GOOGLE, settings.gemini_model)
        return build_env_fallback_provider()

    try:
        api_key = EncryptionService(EncryptionKeyRepository(db)).decrypt(
            config.api_key_encrypted, config.encryption_key_id,
        )
    except Exception:
        logger.exception(
            "LLM provider: could not decrypt the saved %s API key; falling back to .env Gemini.",
            config.provider,
        )
        return build_env_fallback_provider()

    logger.info("LLM provider: saved config (provider=%s model=%s)", config.provider, config.model_name)
    return build_provider(config.provider, api_key, config.model_name)
