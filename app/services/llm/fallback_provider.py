"""
Main model -> backup model failover within one provider (same API key).

Only failures a *different model* can get around trigger the backup: the
main model's own quota/limit is used up, it's retired or not enabled for
the key, it's overloaded/unreachable, or it returned unusable output.
Failures that would hit the backup identically - an invalid key, an account
out of credits, a malformed request, a refusal - are raised as-is.

When the main model fails that way it is marked "down" in Redis for a while
(how long depends on why), so the following calls go straight to the backup
instead of each paying for a doomed request first. Redis being unavailable
just means no marker: every call tries the main model first (fail open).
"""
import logging

from pydantic import BaseModel

from app.core.cache_keys import llm_model_down_key
from app.services.cache_service import CacheService
from app.services.llm.base import LLMError, LLMErrorReason, LLMProvider, ModelInfo

logger = logging.getLogger(__name__)

# reason -> how long (seconds) to skip the main model after it fails this way.
# 0 = use the backup for this call, but don't skip the main model next time.
_FALLBACK_REASONS: dict[str, int] = {
    LLMErrorReason.DAILY_LIMIT: 1800,
    LLMErrorReason.QUOTA_EXHAUSTED: 1800,
    LLMErrorReason.MODEL_NOT_FOUND: 1800,
    LLMErrorReason.NO_ACCESS: 1800,
    LLMErrorReason.RATE_LIMITED: 60,      # replaced by the provider's own retry hint when it gives one
    LLMErrorReason.UNAVAILABLE: 60,
    LLMErrorReason.BAD_OUTPUT: 0,
}
_MAX_RATE_LIMIT_SKIP_SECONDS = 300


def _skip_seconds(exc: LLMError) -> int:
    seconds = _FALLBACK_REASONS.get(exc.reason, 0)
    if exc.reason == LLMErrorReason.RATE_LIMITED and exc.retry_after_seconds:
        seconds = min(max(int(exc.retry_after_seconds) + 1, 1), _MAX_RATE_LIMIT_SKIP_SECONDS)
    return seconds


class FallbackLLMProvider:
    """
    An LLMProvider that runs `primary` and falls back to `fallback`. After
    each generate_json, `last_model_used` / `last_used_fallback` say which
    model actually answered (for logs and audit).
    """

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, cache: CacheService | None = None):
        self.primary = primary
        self.fallback = fallback
        self.cache = cache
        self.provider_name = primary.provider_name
        self.last_model_used: str | None = None
        self.last_used_fallback = False

    @property
    def model(self) -> str:
        return self.primary.model

    def generate_json(self, prompt: str, response_schema: type[BaseModel], *, deterministic: bool = False) -> dict:
        if self._is_down(self.primary):
            logger.info(
                "LLM: %s/%s is marked unavailable - using backup model %s.",
                self.provider_name, self.primary.model, self.fallback.model,
            )
            try:
                return self._run(self.fallback, prompt, response_schema, used_fallback=True, deterministic=deterministic)
            except LLMError as fallback_exc:
                if fallback_exc.reason not in _FALLBACK_REASONS:
                    raise
                # The backup is failing too - the main model may have
                # recovered since it was marked, so give it one more try.
                logger.warning(
                    "LLM: backup model %s failed (%s); retrying main model %s.",
                    self.fallback.model, fallback_exc.reason, self.primary.model,
                )
                return self._run(self.primary, prompt, response_schema, used_fallback=False, deterministic=deterministic)

        try:
            return self._run(self.primary, prompt, response_schema, used_fallback=False, deterministic=deterministic)
        except LLMError as exc:
            if exc.reason not in _FALLBACK_REASONS:
                raise
            self._mark_down(self.primary, exc)
            logger.warning(
                "LLM: main model %s/%s failed (%s) - falling back to %s.",
                self.provider_name, self.primary.model, exc.reason, self.fallback.model,
            )
            try:
                return self._run(self.fallback, prompt, response_schema, used_fallback=True, deterministic=deterministic)
            except LLMError as fallback_exc:
                raise fallback_exc from exc

    def list_models(self) -> list[ModelInfo]:
        return self.primary.list_models()

    def _run(
        self, provider: LLMProvider, prompt: str, response_schema, *, used_fallback: bool, deterministic: bool = False,
    ) -> dict:
        if deterministic:
            result = provider.generate_json(prompt, response_schema, deterministic=True)
        else:
            # Plain call kept for providers/fakes without the keyword.
            result = provider.generate_json(prompt, response_schema)
        self.last_model_used = provider.model
        self.last_used_fallback = used_fallback
        return result

    def _is_down(self, provider: LLMProvider) -> bool:
        if self.cache is None:
            return False
        return self.cache.get(llm_model_down_key(provider.provider_name, provider.model)) is not None

    def _mark_down(self, provider: LLMProvider, exc: LLMError) -> None:
        seconds = _skip_seconds(exc)
        if self.cache is None or seconds <= 0:
            return
        self.cache.set(llm_model_down_key(provider.provider_name, provider.model), exc.reason, ttl=seconds)
