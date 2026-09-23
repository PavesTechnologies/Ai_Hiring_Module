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

# OpenAI's /models lists embeddings, audio, image, moderation etc. too;
# only these prefixes are chat models that support structured output.
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_NON_CHAT_MARKERS = ("audio", "realtime", "transcribe", "tts", "image", "search", "embedding")


class OpenAIProvider:
    provider_name = LLMProviderName.OPENAI

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        base_url: str | None = None,
    ):
        self.model = model
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def generate_json(self, prompt: str, response_schema: type[BaseModel]) -> dict:
        try:
            # chat.completions.parse() converts the Pydantic schema to a
            # strict json_schema response_format and validates the reply.
            completion = self.client.chat.completions.parse(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format=response_schema,
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

    def list_models(self) -> list[ModelInfo]:
        try:
            ids = sorted(m.id for m in self.client.models.list() if self._is_chat_model(m.id))
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
    def _is_chat_model(model_id: str) -> bool:
        return model_id.startswith(_CHAT_MODEL_PREFIXES) and not any(m in model_id for m in _NON_CHAT_MARKERS)


class GroqProvider(OpenAIProvider):
    """
    Groq serves an OpenAI-compatible API, so it reuses the OpenAI SDK with
    Groq's base URL. Strict json_schema output is only supported on a few
    Groq models, so this uses JSON mode (json_object) and puts the schema
    in the prompt instead; the callers' *Response models still validate
    the result exactly as they do for every other provider.
    """
    provider_name = LLMProviderName.GROQ

    _BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str, model: str, timeout_seconds: float, max_retries: int):
        super().__init__(api_key, model, timeout_seconds, max_retries, base_url=self._BASE_URL)

    @property
    def _label(self) -> str:
        return "Groq"

    def generate_json(self, prompt: str, response_schema: type[BaseModel]) -> dict:
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
                response_format={"type": "json_object"},
            )
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from exc
        except openai.APIConnectionError as exc:
            raise LLMTransientError(f"Groq connection error: {exc}") from exc

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise LLMPermanentError("Groq response was truncated (finish_reason=length).", reason=LLMErrorReason.BAD_OUTPUT)
        try:
            return json.loads(choice.message.content or "")
        except json.JSONDecodeError as e:
            raise LLMPermanentError(f"Groq returned invalid JSON: {e}", reason=LLMErrorReason.BAD_OUTPUT) from e

    @staticmethod
    def _is_chat_model(model_id: str) -> bool:
        # Groq's catalogue is chat models plus a few speech/guard models.
        return not any(m in model_id for m in ("whisper", "tts", "guard", "prompt-guard"))
