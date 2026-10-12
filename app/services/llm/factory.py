import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMProvider, LLMProviderName
from app.services.llm.capabilities import load_capability_rules, resolve_capabilities
from app.services.llm.fallback_provider import FallbackLLMProvider
from app.services.llm.gemini_provider import GeminiProvider
from app.services.llm.model_selection import LLMPurpose, select_assignment
from app.services.llm.openai_provider import GroqProvider, OpenAIProvider

logger = logging.getLogger(__name__)

AI_PROVIDER_KEY_PURPOSE = "AI_PROVIDER_KEY"


def build_provider(
    provider: str,
    api_key: str,
    model: str,
    *,
    timeout_seconds: float | None = None,
    capability_rules: list[dict] | None = None,
) -> LLMProvider:
    """
    Builds a provider client from explicit values - used both for the saved
    config and for the Settings "Check" button's unsaved values.
    `capability_rules` (platform_config LLM_MODEL_CAPABILITIES) decides
    which request settings the adapter sends for this model; None uses the
    built-in defaults (see capabilities.py).
    """
    timeout = timeout_seconds or settings.gemini_timeout_ms / 1000
    capabilities = resolve_capabilities(provider, model, capability_rules)
    if provider == LLMProviderName.GOOGLE:
        return GeminiProvider(api_key=api_key, model=model, timeout_ms=int(timeout * 1000),
                              capabilities=capabilities)
    if provider == LLMProviderName.ANTHROPIC:
        return AnthropicProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                                 max_retries=settings.llm_max_retries, capabilities=capabilities)
    if provider == LLMProviderName.OPENAI:
        return OpenAIProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                              max_retries=settings.llm_max_retries, capabilities=capabilities)
    if provider == LLMProviderName.GROQ:
        return GroqProvider(api_key=api_key, model=model, timeout_seconds=timeout,
                            max_retries=settings.llm_max_retries, capabilities=capabilities)
    raise LLMPermanentError(f"Unsupported AI provider '{provider}'.", reason=LLMErrorReason.BAD_REQUEST)


def build_env_fallback_provider() -> LLMProvider:
    """The pre-existing behaviour: Gemini configured from .env."""
    return GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model)


def _model_cache():
    """Redis-backed marker store for FallbackLLMProvider; None (no skipping) if Redis can't even be set up."""
    try:
        from app.core.redis_client import get_redis_client
        from app.services.cache_service import CacheService
        return CacheService(get_redis_client())
    except Exception:
        logger.warning("LLM provider: Redis unavailable - backup models work, but a down model isn't remembered.")
        return None


def resolve_active_provider(db: Session, purpose: str = LLMPurpose.EXTRACTION) -> LLMProvider:
    """
    `purpose` (LLMPurpose.EXTRACTION for JD/resume parsing, EVALUATION for
    AI evaluation) picks which of the provider's models runs - the one
    assigned to that purpose, else its Both model (see model_selection).
    When that assignment has a backup model, the result is a
    FallbackLLMProvider over the two.

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
    return _provider_from_config(db, config, purpose)


def resolve_provider(db: Session, provider_name: str, purpose: str = LLMPurpose.EXTRACTION) -> LLMProvider:
    """
    The saved config for one named provider, active or not - for tools that
    compare providers on the same input (scripts/skill_detection_live_runs.py).
    Raises when that provider has no verified saved config.
    """
    config = AIProviderConfigRepository(db).get_by_provider(provider_name)
    if config is None or not config.is_verified:
        raise LLMPermanentError(f"No verified saved config for provider '{provider_name}'.",
                                reason=LLMErrorReason.BAD_REQUEST)
    return _provider_from_config(db, config, purpose)


def _provider_from_config(db: Session, config, purpose: str) -> LLMProvider:
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

    assignment = select_assignment(getattr(config, "models", None) or [], purpose, default_model=config.model_name)
    rules = load_capability_rules(db)
    primary = build_provider(config.provider, api_key, assignment.model_name, capability_rules=rules)
    if not assignment.fallback_model_name:
        logger.info("LLM provider: saved config (provider=%s purpose=%s model=%s)",
                    config.provider, purpose, assignment.model_name)
        return primary

    logger.info("LLM provider: saved config (provider=%s purpose=%s model=%s backup=%s)",
                config.provider, purpose, assignment.model_name, assignment.fallback_model_name)
    return FallbackLLMProvider(
        primary,
        build_provider(config.provider, api_key, assignment.fallback_model_name, capability_rules=rules),
        _model_cache(),
    )
