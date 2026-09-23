import logging
import time
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.encryption_service import EncryptionService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableError,
)
from app.models.ai_provider import AIProviderConfig
from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
from app.schemas.ai_provider.ai_provider_schema import (
    ActiveProviderResponse,
    AIProviderListResponse,
    AIProviderRowResponse,
    CreateAIProviderRequest,
    ListModelsRequest,
    ModelOptionResponse,
    ProviderOptionResponse,
    UpdateAIProviderRequest,
    VerifyRequest,
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
    LLMErrorReason.RATE_LIMITED: "The provider is limiting how often this key can be used (free-tier keys allow only a few requests per minute). Wait a minute and try again.",
    LLMErrorReason.UNAVAILABLE: "The provider couldn't be reached or is busy. Try again in a moment.",
    LLMErrorReason.BAD_OUTPUT: "This model couldn't return the structured results AIRS needs. Pick another model.",
    LLMErrorReason.REFUSED: "This model declined the test request. Pick another model.",
    LLMErrorReason.BAD_REQUEST: "The provider rejected the request for this model. Pick another model.",
}
_FALLBACK_MESSAGE = "Something went wrong while contacting the provider. Try again, or pick another model."
_SAVE_FAILED = "Couldn't save the AI provider right now. Please try again."


def _friendly_error(exc: Exception, *, provider: str, action: str) -> str:
    reason = getattr(exc, "reason", None) if isinstance(exc, LLMError) else None
    logger.warning(
        "AI provider %s failed (provider=%s reason=%s): %s",
        action, provider, reason or type(exc).__name__, exc,
    )
    return _FRIENDLY_MESSAGES.get(reason, _FALLBACK_MESSAGE)


def _already_registered(provider: str) -> ConflictError:
    return ConflictError(f"{LLMProviderName.LABELS[provider]} is already registered. Edit it instead.")


class AIProviderConfigService:
    """
    Settings -> AI model providers: several providers can be registered (one
    row per provider) and exactly one is active for processing. Every failure
    surfaces as a plain-English message; raw provider/DB errors only go to
    the log.
    """

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

    def list_provider_options(self) -> list[ProviderOptionResponse]:
        registered = self.repository.registered_providers()
        return [
            ProviderOptionResponse(key=key, label=LLMProviderName.LABELS[key], registered=key in registered)
            for key in LLMProviderName.ALL
        ]

    def list_registered(self, *, page: int, page_size: int) -> AIProviderListResponse:
        rows = self.repository.list(page=page, page_size=page_size)
        return AIProviderListResponse(
            items=[self._to_row(row) for row in rows],
            page=page,
            page_size=page_size,
            total=self.repository.count(),
        )

    def get_active_provider(self) -> ActiveProviderResponse:
        config = self.repository.get_active()
        if config is not None and config.is_verified:
            return ActiveProviderResponse(
                source="database",
                provider=config.provider,
                provider_label=LLMProviderName.LABELS.get(config.provider, config.provider),
                model_name=config.model_name,
            )
        return ActiveProviderResponse(
            source="env_fallback",
            provider=LLMProviderName.GOOGLE,
            provider_label=LLMProviderName.LABELS[LLMProviderName.GOOGLE],
            model_name=settings.gemini_model,
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

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        api_key = self._resolve_api_key(request.provider, request.api_key)
        return self._verify_with_key(request.provider, request.model_name, api_key)

    # ── Writes ────────────────────────────────────────────────────────

    def create(
        self, request: CreateAIProviderRequest, *, updated_by: str, actor_role: str | None,
    ) -> AIProviderRowResponse:
        if self.repository.get_by_provider(request.provider) is not None:
            raise _already_registered(request.provider)

        # Re-verified server-side: the client's own "Check passed" state is
        # never trusted, so an unusable model can't be registered.
        self._require_verified(request.provider, request.model_name, request.api_key)
        ciphertext, key_id = self._encrypt(request.api_key)
        # The first provider registered (or any, while none is active)
        # becomes the one processing uses.
        make_active = self.repository.get_active() is None

        try:
            row = self.repository.create(AIProviderConfig(
                provider=request.provider,
                model_name=request.model_name,
                api_key_encrypted=ciphertext,
                encryption_key_id=key_id,
                api_key_last4=request.api_key[-4:],
                is_active=make_active,
                is_verified=True,
                verified_at=datetime.now(timezone.utc),
                updated_by=updated_by,
            ))
            self._audit(ActionType.AI_PROVIDER_CONFIG_CREATED, row, updated_by, actor_role, {
                "title": "AI provider registered",
                "after": {"provider": row.provider, "model_name": row.model_name, "is_active": row.is_active},
            })
            self.repository.commit()
        except IntegrityError:
            # Two admins registering the same provider at the same moment.
            self.repository.rollback()
            raise _already_registered(request.provider)
        except Exception:
            self._fail("Registering the AI provider failed.")
        return self._to_row(row)

    def update(
        self, config_id: uuid.UUID, request: UpdateAIProviderRequest, *, updated_by: str, actor_role: str | None,
    ) -> AIProviderRowResponse:
        row = self._get_row(config_id)
        api_key = request.api_key or self._decrypt(row)
        self._require_verified(row.provider, request.model_name, api_key)
        new_key = self._encrypt(request.api_key) if request.api_key else None
        before_model = row.model_name

        try:
            row.model_name = request.model_name
            if new_key is not None:
                row.api_key_encrypted, row.encryption_key_id = new_key
                row.api_key_last4 = request.api_key[-4:]
            row.is_verified = True
            row.verified_at = datetime.now(timezone.utc)
            row.last_error = None
            row.updated_by = updated_by
            row = self.repository.update(row)
            self._audit(ActionType.AI_PROVIDER_CONFIG_UPDATED, row, updated_by, actor_role, {
                "title": "AI provider updated",
                "provider": row.provider,
                "before": {"model_name": before_model},
                "after": {"model_name": row.model_name, "api_key_replaced": new_key is not None},
            })
            self.repository.commit()
        except Exception:
            self._fail("Updating the AI provider failed.")
        return self._to_row(row)

    def activate(self, config_id: uuid.UUID, *, updated_by: str, actor_role: str | None) -> AIProviderRowResponse:
        row = self._get_row(config_id)
        if row.is_active:
            return self._to_row(row)
        if not row.is_verified:
            raise ConflictError("Edit this provider and check it again before making it active.")

        previous = self.repository.get_active()
        previous_details = (
            {"provider": previous.provider, "model_name": previous.model_name} if previous else None
        )
        try:
            row = self.repository.set_active(row)
            self._audit(ActionType.AI_PROVIDER_CONFIG_ACTIVATED, row, updated_by, actor_role, {
                "title": "Active AI provider changed",
                "before": previous_details,
                "after": {"provider": row.provider, "model_name": row.model_name},
            })
            self.repository.commit()
        except Exception:
            self._fail("Activating the AI provider failed.")
        return self._to_row(row)

    def delete(self, config_id: uuid.UUID, *, updated_by: str, actor_role: str | None) -> None:
        row = self._get_row(config_id)
        if self.repository.count() <= 1:
            raise ConflictError("At least one AI provider must remain, so the last one can't be deleted.")
        if row.is_active:
            raise ConflictError("This is the active provider. Set another provider as active first.")

        try:
            self._audit(ActionType.AI_PROVIDER_CONFIG_DELETED, row, updated_by, actor_role, {
                "title": "AI provider deleted",
                "before": {"provider": row.provider, "model_name": row.model_name},
            })
            self.repository.delete(row)
            self.repository.commit()
        except Exception:
            self._fail("Deleting the AI provider failed.")

    # ── Helpers ───────────────────────────────────────────────────────

    def _get_row(self, config_id: uuid.UUID) -> AIProviderConfig:
        row = self.repository.get_by_id(config_id)
        if row is None:
            raise NotFoundError("This AI provider no longer exists. Refresh the page and try again.")
        return row

    def _resolve_api_key(self, provider: str, api_key: str | None) -> str:
        """
        A typed key always wins. Without one, reuse the key saved for *this*
        provider - a Google key is never sent to Anthropic.
        """
        if api_key:
            return api_key
        row = self.repository.get_by_provider(provider)
        if row is not None:
            return self._decrypt(row)
        raise UnprocessableError("Enter an API key for this provider.")

    def _decrypt(self, row: AIProviderConfig) -> str:
        try:
            return self.encryption_service.decrypt(row.api_key_encrypted, row.encryption_key_id)
        except Exception:
            logger.exception("Decrypting the saved AI provider API key failed (provider=%s).", row.provider)
            raise ServiceUnavailableError("The saved API key couldn't be read. Please enter the API key again.")

    def _encrypt(self, api_key: str) -> tuple[bytes, uuid.UUID]:
        try:
            return self.encryption_service.encrypt(api_key, AI_PROVIDER_KEY_PURPOSE)
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

    def _require_verified(self, provider: str, model_name: str, api_key: str) -> None:
        result = self._verify_with_key(provider, model_name, api_key)
        if not result.verified:
            raise UnprocessableError(result.message)

    def _audit(
        self, action: ActionType, row: AIProviderConfig, actor_id: str, actor_role: str | None, details: dict,
    ) -> None:
        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=action.value,
            entity_type=EntityType.AI_PROVIDER_CONFIG.value,
            entity_id=row.id,
            details=details,
        )

    def _fail(self, log_message: str):
        """Rolls back, logs the real cause, and raises a friendly 503."""
        self.repository.rollback()
        logger.exception(log_message)
        raise ServiceUnavailableError(_SAVE_FAILED)

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
    def _to_row(row: AIProviderConfig) -> AIProviderRowResponse:
        return AIProviderRowResponse(
            id=row.id,
            provider=row.provider,
            provider_label=LLMProviderName.LABELS.get(row.provider, row.provider),
            model_name=row.model_name,
            api_key_masked=_mask(row.api_key_last4),
            is_active=row.is_active,
            is_verified=row.is_verified,
            verified_at=row.verified_at,
            updated_at=row.updated_at,
        )
