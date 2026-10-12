import json

import openai
from pydantic import BaseModel, ValidationError

from app.services.llm.base import (
    LLMErrorReason,
    LLMPermanentError,
    LLMProviderName,
    LLMTransientError,
    ModelInfo,
    status_error,
)
from app.services.llm.capabilities import ModelCapabilities, resolve_capabilities

# OpenAI's /models lists every model the key can see - embeddings, audio,
# image, moderation, and models that only work on other endpoints. Only
# chat-completions models that support structured output are usable here.
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_NON_CHAT_MARKERS = (
    "audio", "realtime", "transcribe", "tts", "image", "search", "embedding",
    "moderation", "instruct",      # instruct = legacy completions endpoint only
    "codex", "deep-research",      # Responses-API-only models
    "computer-use",
)
# "-pro" variants (o1-pro, o3-pro, gpt-5-pro) are Responses-API only.
_RESPONSES_ONLY_SUFFIXES = ("-pro",)

# Groq: speech (whisper, orpheus/playai text-to-speech) and safety-classifier
# (guard/safeguard) models can't do text extraction.
_GROQ_NON_TEXT_MARKERS = ("whisper", "tts", "orpheus", "playai", "guard")
# Groq's error code when its JSON mode can't produce a valid document.
_GROQ_JSON_FAILED = "json_validate_failed"
# A resume plus the extraction prompt and JSON schema needs far more than a
# 4K window; smaller models fail on real documents even if Check passes.
_MIN_CONTEXT_WINDOW = 16_000


class OpenAIProvider:
    provider_name = LLMProviderName.OPENAI

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        base_url: str | None = None,
        capabilities: ModelCapabilities | None = None,
    ):
        self.model = model
        self.capabilities = capabilities or resolve_capabilities(self.provider_name, model)
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def _settings_kwargs(self, deterministic: bool) -> dict:
        """Only the settings this model supports (see capabilities.py)."""
        kwargs = {}
        if deterministic and self.capabilities.supports_temperature:
            kwargs["temperature"] = 0
        if self.capabilities.max_output_tokens:
            kwargs["max_completion_tokens"] = self.capabilities.max_output_tokens
        return kwargs

    def generate_json(self, prompt: str, response_schema: type[BaseModel], *, deterministic: bool = False) -> dict:
        if self.capabilities.structured_output != "json_schema":
            return self._generate_json_mode(prompt, response_schema, deterministic)
        try:
            # chat.completions.parse() converts the Pydantic schema to a
            # strict json_schema response_format and validates the reply.
            completion = self.client.chat.completions.parse(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format=response_schema,
                **self._settings_kwargs(deterministic),
            )
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
            raise LLMPermanentError(f"{self._label} response was cut off: {exc}", reason=LLMErrorReason.BAD_OUTPUT) from exc
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from exc
        except openai.APIConnectionError as exc:
            raise LLMTransientError(f"{self._label} connection error: {exc}") from exc
        except ValidationError as exc:
            raise LLMPermanentError(f"{self._label} returned JSON that does not match the schema: {exc}", reason=LLMErrorReason.BAD_OUTPUT) from exc

        message = completion.choices[0].message
        if message.refusal:
            raise LLMPermanentError(f"{self._label} declined the request: {message.refusal}", reason=LLMErrorReason.REFUSED)
        if message.parsed is None:
            raise LLMPermanentError(f"{self._label} returned no structured output.", reason=LLMErrorReason.BAD_OUTPUT)
        return message.parsed.model_dump(mode="json")

    def _response_format(self, response_schema: type[BaseModel]) -> dict:
        if self.capabilities.structured_output == "json_schema":
            # Non-strict: strict mode needs every field required and no
            # defaults, which the extraction schemas don't follow.
            return {"type": "json_schema", "json_schema": {
                "name": response_schema.__name__, "schema": response_schema.model_json_schema(), "strict": False,
            }}
        return {"type": "json_object"}

    def _generate_json_mode(self, prompt: str, response_schema: type[BaseModel], deterministic: bool) -> dict:
        """
        JSON mode (json_object, schema in the prompt) or non-strict
        json_schema via create() - for models without strict structured
        output. The callers' *Response models validate the result.
        """
        schema = json.dumps(response_schema.model_json_schema())
        system = (
            "Respond with a single JSON object only, no prose. "
            f"It must conform to this JSON schema:\n{schema}"
        )
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format=self._response_format(response_schema),
                **self._settings_kwargs(deterministic),
            )
        except openai.APIStatusError as exc:
            if _GROQ_JSON_FAILED in str(exc):
                # Groq's JSON mode rejects its own unfinished/invalid JSON as
                # a 400 - that's the model's output failing, not our
                # request, so it's BAD_OUTPUT (a backup model may succeed)
                # rather than BAD_REQUEST.
                raise LLMPermanentError(
                    f"{self._label} returned invalid JSON: {exc}", reason=LLMErrorReason.BAD_OUTPUT,
                ) from exc
            raise self._map_status_error(exc) from exc
        except openai.APIConnectionError as exc:
            raise LLMTransientError(f"{self._label} connection error: {exc}") from exc

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise LLMPermanentError(
                f"{self._label} response was truncated (finish_reason=length).", reason=LLMErrorReason.BAD_OUTPUT,
            )
        try:
            return json.loads(choice.message.content or "")
        except json.JSONDecodeError as e:
            raise LLMPermanentError(f"{self._label} returned invalid JSON: {e}", reason=LLMErrorReason.BAD_OUTPUT) from e

    def list_models(self) -> list[ModelInfo]:
        try:
            ids = sorted(m.id for m in self.client.models.list() if self._is_usable(m))
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from exc
        except openai.APIConnectionError as exc:
            raise LLMTransientError(f"{self._label} connection error: {exc}") from exc
        return [ModelInfo(id=model_id, display_name=model_id) for model_id in ids]

    @property
    def _label(self) -> str:
        return "OpenAI"

    def _map_status_error(self, exc: openai.APIStatusError):
        return status_error(exc.status_code, f"{self._label} API error: {exc}")

    @staticmethod
    def _is_usable(model) -> bool:
        model_id = model.id
        return (
            model_id.startswith(_CHAT_MODEL_PREFIXES)
            and not any(m in model_id for m in _NON_CHAT_MARKERS)
            and not any(model_id.endswith(s) or f"{s}-" in model_id for s in _RESPONSES_ONLY_SUFFIXES)
        )


class GroqProvider(OpenAIProvider):
    """
    Groq serves an OpenAI-compatible API, so it reuses the OpenAI SDK with
    Groq's base URL. Strict json_schema output is only supported on a few
    Groq models, so the capability map normally selects JSON mode
    (json_object, schema in the prompt) or non-strict json_schema - both
    via OpenAIProvider._generate_json_mode; the callers' *Response models
    still validate the result exactly as they do for every other provider.
    """
    provider_name = LLMProviderName.GROQ

    _BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(
        self, api_key: str, model: str, timeout_seconds: float, max_retries: int,
        capabilities: ModelCapabilities | None = None,
    ):
        super().__init__(api_key, model, timeout_seconds, max_retries, base_url=self._BASE_URL,
                         capabilities=capabilities)

    @property
    def _label(self) -> str:
        return "Groq"

    def generate_json(self, prompt: str, response_schema: type[BaseModel], *, deterministic: bool = False) -> dict:
        # Never the strict parse() path - Groq's json_schema is non-strict.
        return self._generate_json_mode(prompt, response_schema, deterministic)

    @staticmethod
    def _is_usable(model) -> bool:
        # Groq's /models carries `active` and `context_window` (not part of
        # the OpenAI SDK type, so read defensively).
        if getattr(model, "active", True) is False:
            return False
        context_window = getattr(model, "context_window", None)
        if isinstance(context_window, int) and context_window < _MIN_CONTEXT_WINDOW:
            return False
        return not any(m in model.id for m in _GROQ_NON_TEXT_MARKERS)
