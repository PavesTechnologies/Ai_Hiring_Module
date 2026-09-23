import json

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from app.core.config import settings
from app.services.llm.base import (
    LLMErrorReason,
    LLMPermanentError,
    LLMProviderName,
    LLMTransientError,
    ModelInfo,
    status_error,
)

# Status codes retried inside the SDK: the provider being unavailable or
# overloaded. Re-sending the same request shortly after is the correct
# response to these, and they describe the request, not the document.
#
# 429 is deliberately NOT here. Gemini returns 429 for two very different
# things and the HTTP layer cannot tell them apart: short-term rate
# limiting (worth retrying) and RESOURCE_EXHAUSTED quota/billing
# exhaustion (retrying cannot succeed, and each attempt spends another
# call against a quota that is already gone). 429 is mapped one level up
# (status_error), which can read the message and tell the two apart.
_RETRYABLE_STATUS_CODES = [500, 502, 503, 504]


def _build_http_options(timeout_ms: int) -> types.HttpOptions:
    """
    Transport-level retry + timeout for every Gemini call.

    This is the layer a 503 belongs at. Without it the only retry in the
    system was retry_policy/RetryDriver, which re-runs whole pipeline
    stages: one 503 in AI_EXTRACTION cost a fresh TEXT_EXTRACTION,
    TEXT_CLEANING, PII_DETECTION and PII_REDACTION (observed live — 5 AI
    attempts produced 25 stage-execution rows, and PII detection ran on
    the same document five times), with 10-120s of pipeline backoff for a
    blip that clears in seconds. Retrying here re-sends only the failed
    request and redoes nothing else.

    RetryDriver is still the backstop for a genuine outage that outlasts
    these attempts — the two layers are complementary, not redundant.
    """
    return types.HttpOptions(
        timeout=timeout_ms,
        retry_options=types.HttpRetryOptions(
            attempts=settings.gemini_retry_attempts,
            initial_delay=settings.gemini_retry_initial_delay,
            max_delay=settings.gemini_retry_max_delay,
            exp_base=settings.gemini_retry_exp_base,
            jitter=settings.gemini_retry_jitter,
            http_status_codes=_RETRYABLE_STATUS_CODES,
        ),
    )


class GeminiProvider:
    provider_name = LLMProviderName.GOOGLE

    def __init__(self, api_key: str, model: str, timeout_ms: int | None = None):
        self.model = model
        self.client = genai.Client(
            api_key=api_key,
            http_options=_build_http_options(timeout_ms or settings.gemini_timeout_ms),
        )

    def generate_json(self, prompt: str, response_schema: type[BaseModel]) -> dict:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": response_schema,
                },
            )
        except genai_errors.APIError as exc:
            raise self._map_error(exc) from exc

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as e:
            raise LLMPermanentError(f"Gemini returned invalid JSON: {e}", reason=LLMErrorReason.BAD_OUTPUT) from e

    def list_models(self) -> list[ModelInfo]:
        try:
            models = []
            for m in self.client.models.list():
                actions = m.supported_actions or []
                if "generateContent" not in actions:
                    continue
                model_id = (m.name or "").removeprefix("models/")
                models.append(ModelInfo(id=model_id, display_name=m.display_name or model_id))
            return models
        except genai_errors.APIError as exc:
            raise self._map_error(exc) from exc

    @staticmethod
    def _map_error(exc: genai_errors.APIError):
        status_code = getattr(exc, "code", None)
        if isinstance(exc, genai_errors.ServerError):
            return LLMTransientError(f"Gemini API error: {exc}", status_code=status_code)
        return status_error(status_code, f"Gemini API error: {exc}")
