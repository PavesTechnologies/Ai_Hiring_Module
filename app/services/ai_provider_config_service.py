import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import (
    BadRequestError,
    ServiceUnavailableError,
    UnprocessableError,
)
from app.models.ai_provider import AIProviderConfig
from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
from app.schemas.ai_provider.ai_provider_schema import (
    AIProviderConfigRequest,
    AIProviderConfigResponse,
    ListModelsRequest,
    ModelOptionResponse,
    ProviderOptionResponse,
    VerifyResponse,
)
from app.services.audit_service import AuditService
from app.services.llm.base import LLMError, LLMErrorReason, LLMProviderName
from app.services.llm.factory import AI_PROVIDER_KEY_PURPOSE, build_provider

logger = logging.getLogger(__name__)


class _VerifySchema(BaseModel):
    ok: bool


# One tiny structured-output call: proves the key is valid, the model
# exists for this key, and the model can return schema-shaped JSON - the
# three things every real extraction call depends on.
_VERIFY_PROMPT = 'This is a connectivity check. Reply with JSON: {"ok": true}'


def _mask(last4: str | None) -> str | None:
    return f"••••{last4}" if last4 else None


# The only text an admin ever sees for a provider failure. Raw SDK error
# bodies (status JSON, request ids, doc links) go to the server log, never
# to the UI.
_FRIENDLY_MESSAGES = {
    LLMErrorReason.INVALID_KEY: "The API key is invalid. Check the key and try again.",
    LLMErrorReason.NO_ACCESS: "This API key doesn't have permission to use this provider or model.",
    LLMErrorReason.MODEL_NOT_FOUND: "This model isn't available for this API key. Pick another model.",
    LLMErrorReason.QUOTA_EXHAUSTED: "This API key has run out of quota or credits. Check the billing on the provider's account.",
    LLMErrorReason.RATE_LIMITED: "The provider is rate-limiting this key right now. Wait a moment and try again.",
    LLMErrorReason.UNAVAILABLE: "The provider couldn't be reached or is busy. Try again in a moment.",
    LLMErrorReason.BAD_OUTPUT: "This model couldn't return the structured results AIRS needs. Pick another model.",
    LLMErrorReason.REFUSED: "This model declined the test request. Pick another model.",
    LLMErrorReason.BAD_REQUEST: "The provider rejected the request for this model. Pick another model.",
}
_FALLBACK_MESSAGE = "Something went wrong while contacting the provider. Try again, or pick another model."


def _friendly_error(exc: Exception, *, provider: str, action: str) -> str:
    reason = getattr(exc, "reason", None) if isinstance(exc, LLMError) else None
    logger.warning(
        "AI provider %s failed (provider=%s reason=%s): %s",
        action, provider, reason or type(exc).__name__, exc,
    )
    return _FRIENDLY_MESSAGES.get(reason, _FALLBACK_MESSAGE)


class AIProviderConfigService:

    def __init__(
        self,
        repository: AIProviderConfigRepository,
        encryption_service: EncryptionService,
        audit_service: AuditService,
    ):
        self.repository = repository
        self.encryption_service = encryption_service
        self.audit_service = audit_service

    # ── Reads ─────────────────────────────────────────────────────────

    @staticmethod
    def list_providers() -> list[ProviderOptionResponse]:
        return [
            ProviderOptionResponse(key=key, label=LLMProviderName.LABELS[key])
            for key in LLMProviderName.ALL
        ]

    def get_current(self) -> AIProviderConfigResponse:
        config = self.repository.get_active()
        if config is not None and config.is_verified:
            return self._to_response(config)

        env_key = settings.gemini_api_key
        return AIProviderConfigResponse(
            source="env_fallback",
            provider=LLMProviderName.GOOGLE,
            provider_label=LLMProviderName.LABELS[LLMProviderName.GOOGLE],
            model_name=settings.gemini_model,
            api_key_masked=_mask(env_key[-4:]) if env_key else None,
        )

    def list_models(self, request: ListModelsRequest) -> list[ModelOptionResponse]:
        api_key = self._resolve_api_key(request.provider, request.api_key)
        try:
            provider = build_provider(
                request.provider, api_key, model="",
                timeout_seconds=settings.llm_verify_timeout_seconds,
            )
            models = provider.list_models()
        except Exception as exc:
            # Exception, not just LLMError: SDK client construction can raise
            # its own errors (e.g. genai rejects an empty key with ValueError).
            raise BadRequestError(
                f"Couldn't load models. {_friendly_error(exc, provider=request.provider, action='model list')}"
            )
        return [ModelOptionResponse(id=m.id, display_name=m.display_name) for m in models]

    # ── Verify / save / reset ─────────────────────────────────────────

    def verify(self, request: AIProviderConfigRequest) -> VerifyResponse:
        api_key = self._resolve_api_key(request.provider, request.api_key)
        return self._verify_with_key(request.provider, request.model_name, api_key)

    def save(self, request: AIProviderConfigRequest, *, updated_by: str, actor_role: str | None) -> AIProviderConfigResponse:
        api_key = self._resolve_api_key(request.provider, request.api_key)

        # Re-verified server-side: the client's own "Check passed" state is
        # never trusted, so an unusable model can't be saved.
        result = self._verify_with_key(request.provider, request.model_name, api_key)
        if not result.verified:
            raise UnprocessableError(result.message)

        try:
            ciphertext, key_id = self.encryption_service.encrypt(api_key, AI_PROVIDER_KEY_PURPOSE)
        except Exception:
            # Ops detail (missing seed row / ENCRYPTION_KEY_AI_PROVIDER_V1) is
            # in this log line, not in the message the admin sees.
            logger.exception(
                "Encrypting the AI provider API key failed - is the AI_PROVIDER_KEY encryption key "
                "seeded and ENCRYPTION_KEY_AI_PROVIDER_V1 set?"
            )
            raise ServiceUnavailableError(
                "The API key couldn't be saved securely right now. Please contact your system administrator."
            )

        before = self.repository.get_active()
        before_details = (
            {"provider": before.provider, "model_name": before.model_name} if before else None
        )

        try:
            saved = self.repository.upsert_active(AIProviderConfig(
                provider=request.provider,
                model_name=request.model_name,
                api_key_encrypted=ciphertext,
                encryption_key_id=key_id,
                api_key_last4=api_key[-4:],
                is_active=True,
                is_verified=True,
                verified_at=datetime.now(timezone.utc),
                last_error=None,
                updated_by=updated_by,
            ))
            self.audit_service.log(
                actor_id=updated_by,
                actor_role=actor_role,
                action_type=ActionType.AI_PROVIDER_CONFIG_UPDATED.value,
                entity_type=EntityType.AI_PROVIDER_CONFIG.value,
                entity_id=saved.id,
                details={
                    "title": "AI model provider updated",
                    "before": before_details,
                    "after": {"provider": saved.provider, "model_name": saved.model_name},
                },
            )
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            logger.exception("Saving the AI provider config failed.")
            raise ServiceUnavailableError("Couldn't save the AI model settings right now. Please try again.")

        return self._to_response(saved)

    def reset_to_default(self, *, updated_by: str, actor_role: str | None) -> AIProviderConfigResponse:
        config = self.repository.get_active()
        if config is not None:
            try:
                self.repository.deactivate(config)
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role=actor_role,
                    action_type=ActionType.AI_PROVIDER_CONFIG_DELETED.value,
                    entity_type=EntityType.AI_PROVIDER_CONFIG.value,
                    entity_id=config.id,
                    details={
                        "title": "AI model provider reset to the built-in default",
                        "before": {"provider": config.provider, "model_name": config.model_name},
                    },
                )
                self.repository.commit()
            except Exception:
                self.repository.rollback()
                logger.exception("Resetting the AI provider config failed.")
                raise ServiceUnavailableError("Couldn't reset the AI model settings right now. Please try again.")
        return self.get_current()

    # ── Helpers ───────────────────────────────────────────────────────

    def _resolve_api_key(self, provider: str, api_key: str | None) -> str:
        """
        A typed key always wins. Without one, reuse the saved key - but only
        for the provider it was saved for; a Google key is never sent to
        Anthropic just because the admin switched the dropdown.
        """
        if api_key:
            return api_key

        config = self.repository.get_active()
        if config is not None and config.provider == provider:
            try:
                return self.encryption_service.decrypt(config.api_key_encrypted, config.encryption_key_id)
            except Exception:
                logger.exception("Decrypting the saved AI provider API key failed.")
                raise ServiceUnavailableError("The saved API key couldn't be read. Please enter the API key again.")

        raise UnprocessableError("Enter an API key for this provider.")

    @staticmethod
    def _verify_with_key(provider_key: str, model_name: str, api_key: str) -> VerifyResponse:
        started = time.monotonic()
        try:
            provider = build_provider(
                provider_key, api_key, model_name,
                timeout_seconds=settings.llm_verify_timeout_seconds,
            )
            result = provider.generate_json(_VERIFY_PROMPT, _VerifySchema)
            _VerifySchema.model_validate(result)
        except Exception as exc:
            return VerifyResponse(
                verified=False,
                message=_friendly_error(exc, provider=provider_key, action="check"),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        return VerifyResponse(
            verified=True,
            message=f"{LLMProviderName.LABELS[provider_key]} · {model_name} responded in {latency_ms} ms.",
            latency_ms=latency_ms,
        )

    @staticmethod
    def _to_response(config: AIProviderConfig) -> AIProviderConfigResponse:
        return AIProviderConfigResponse(
            source="database",
            provider=config.provider,
            provider_label=LLMProviderName.LABELS.get(config.provider, config.provider),
            model_name=config.model_name,
            api_key_masked=_mask(config.api_key_last4),
            is_verified=config.is_verified,
            verified_at=config.verified_at,
            updated_by=config.updated_by,
            updated_at=config.updated_at,
        )
