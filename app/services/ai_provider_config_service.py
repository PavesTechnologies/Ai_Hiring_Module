import logging
import time
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.core.cache_keys import ai_provider_verified_key
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
from app.services.cache_service import CacheService
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
    LLMErrorReason.UNAVAILABLE: "The provider couldn't be reached or is busy. Try again in a moment.",
    LLMErrorReason.BAD_OUTPUT: "This model couldn't return the structured results AIRS needs. Pick another model.",
    LLMErrorReason.REFUSED: "This model declined the test request. Pick another model.",
    LLMErrorReason.BAD_REQUEST: "The provider rejected the request for this model. Pick another model.",
}
_FALLBACK_MESSAGE = "Something went wrong while contacting the provider. Try again, or pick another model."
_SAVE_FAILED = "Couldn't save the AI provider right now. Please try again."
# How long a passed Check lets Save skip its own test call.
_VERIFIED_TTL_SECONDS = 300


def _wait_phrase(seconds: float | None) -> str:
    if seconds is None:
        return "a minute"
    seconds = max(1, round(seconds))
    return f"about {seconds} second{'s' if seconds != 1 else ''}"


def _describe_error(exc: Exception, *, provider: str, model: str | None, action: str) -> tuple[str, str]:
    """
    (error_code, plain-English message) for any provider failure. The
    message names the provider/model and uses the limit and retry hint the
    provider sent; the raw reply only goes to the log.
    """
    reason = exc.reason if isinstance(exc, LLMError) else LLMErrorReason.UNKNOWN
    logger.warning(
        "AI provider %s failed (provider=%s model=%s reason=%s): %s",
        action, provider, model, reason if isinstance(exc, LLMError) else type(exc).__name__, exc,
    )

    label = LLMProviderName.LABELS.get(provider, "The provider")
    model_part = f" for {model}" if model else ""
    limit = getattr(exc, "quota_limit", None)

    if reason == LLMErrorReason.RATE_LIMITED:
        limit_part = f" (limit: {limit} requests per minute)" if limit else ""
        message = (
            f"{label} is limiting how often this key can be used{model_part}{limit_part}. "
            f"Wait {_wait_phrase(getattr(exc, 'retry_after_seconds', None))} and try again. "
            "Free-tier keys allow only a few requests per minute."
        )
    elif reason == LLMErrorReason.DAILY_LIMIT:
        limit_part = f" ({limit} requests per day)" if limit else ""
        message = (
            f"This key has used up its daily {label} limit{model_part}{limit_part}. "
            "Try again tomorrow, pick a different model, or enable billing on the provider account for higher limits."
        )
    elif reason == LLMErrorReason.CREDITS_EXHAUSTED:
        message = (
            f"The {label} account for this key has no credits left or has reached its spending limit. "
            "Add credits or raise the limit in the provider's billing settings, then try again."
        )
    elif reason == LLMErrorReason.QUOTA_EXHAUSTED:
        message = (
            f"This key has used up its {label} quota{model_part}. "
            "Check the plan and billing on the provider account, or try again later."
        )
    else:
        message = _FRIENDLY_MESSAGES.get(reason, _FALLBACK_MESSAGE)
    return reason, message


def _with_code(message: str, code: str) -> str:
    return f"{message} (Error code: {code})"


# Well-known key prefixes. Checked before calling the provider so a key
# pasted under the wrong provider gets "this is a Groq key" instead of the
# provider's generic "invalid key". Anthropic's prefix must be tested before
# OpenAI's, since both start with "sk-".
KEY_PROVIDER_MISMATCH = "KEY_PROVIDER_MISMATCH"
_KEY_PREFIXES = (
    (LLMProviderName.ANTHROPIC, "sk-ant-"),
    (LLMProviderName.GROQ, "gsk_"),
    (LLMProviderName.GOOGLE, "AIza"),
    (LLMProviderName.OPENAI, "sk-"),
)


def _article(label: str) -> str:
    return "an" if label[:1].lower() in "aeiou" else "a"


def _key_owner(api_key: str) -> tuple[str, str] | None:
    for provider, prefix in _KEY_PREFIXES:
        if api_key.startswith(prefix):
            return provider, prefix
    return None


def _key_mismatch_message(selected: str, api_key: str) -> str | None:
    """A message when the key clearly belongs to a different provider; None if it fits or is unrecognised."""
    owner = _key_owner(api_key)
    if owner is None or owner[0] == selected:
        return None
    owner_label = LLMProviderName.LABELS[owner[0]]
    selected_label = LLMProviderName.LABELS.get(selected, selected)
    expected = next((prefix for provider, prefix in _KEY_PREFIXES if provider == selected), None)
    expected_part = f' (those start with "{expected}")' if expected else ""
    return (
        f'This looks like {_article(owner_label)} {owner_label} API key (it starts with "{owner[1]}"), but the selected provider is '
        f"{selected_label}. Select {owner_label} as the provider, or paste {_article(selected_label)} {selected_label} key{expected_part}."
    )


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
        cache_service: CacheService | None = None,
    ):
        self.repository = repository
        self.encryption_service = encryption_service
        self.audit_service = audit_service
        # Remembers recent successful Checks so Save doesn't spend another
        # provider request re-checking the exact same values. Optional: with
        # no cache (or Redis down) Save simply checks again.
        self.cache_service = cache_service

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
        mismatch = _key_mismatch_message(request.provider, api_key)
        if mismatch:
            raise BadRequestError(_with_code(f"Couldn't load models. {mismatch}", KEY_PROVIDER_MISMATCH))
        try:
            provider = build_provider(
                request.provider, api_key, model="",
                timeout_seconds=settings.llm_verify_timeout_seconds,
            )
            models = provider.list_models()
        except Exception as exc:
            # Exception, not just LLMError: SDK client construction can raise
            # its own errors (e.g. genai rejects an empty key with ValueError).
            code, message = _describe_error(exc, provider=request.provider, model=None, action="model list")
            raise BadRequestError(_with_code(f"Couldn't load models. {message}", code))
        return [ModelOptionResponse(id=m.id, display_name=m.display_name) for m in models]

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        api_key = self._resolve_api_key(request.provider, request.api_key)
        result = self._verify_with_key(request.provider, request.model_name, api_key)
        if result.verified:
            self._remember_verified(request.provider, request.model_name, api_key)
        return result

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
        """
        Save never trusts the browser's "Check passed" state. But if *this
        server* saw a Check pass for exactly these values within the last few
        minutes, it doesn't call the provider again - on free-tier keys that
        extra call is often what trips the per-minute limit. The marker
        expires after _VERIFIED_TTL_SECONDS, so an old Check never counts.
        """
        if self._recently_verified(provider, model_name, api_key):
            return
        result = self._verify_with_key(provider, model_name, api_key)
        if not result.verified:
            raise UnprocessableError(_with_code(result.message, result.error_code))

    def _remember_verified(self, provider: str, model_name: str, api_key: str) -> None:
        if self.cache_service is None:
            return
        self.cache_service.set(
            ai_provider_verified_key(provider, model_name, api_key),
            "1",
            ttl=_VERIFIED_TTL_SECONDS,
        )

    def _recently_verified(self, provider: str, model_name: str, api_key: str) -> bool:
        if self.cache_service is None:
            return False
        return self.cache_service.get(ai_provider_verified_key(provider, model_name, api_key)) is not None

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
        mismatch = _key_mismatch_message(provider_key, api_key)
        if mismatch:
            return VerifyResponse(verified=False, message=mismatch, error_code=KEY_PROVIDER_MISMATCH)

        started = time.monotonic()
        try:
            provider = build_provider(
                provider_key, api_key, model_name,
                timeout_seconds=settings.llm_verify_timeout_seconds,
            )
            result = provider.generate_json(_VERIFY_PROMPT, _VerifySchema)
            _VerifySchema.model_validate(result)
        except Exception as exc:
            code, message = _describe_error(exc, provider=provider_key, model=model_name, action="check")
            return VerifyResponse(verified=False, message=message, error_code=code)

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
