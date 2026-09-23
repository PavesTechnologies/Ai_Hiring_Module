import anthropic
from pydantic import BaseModel, ValidationError

from app.services.llm.base import (
    LLMErrorReason,
    LLMPermanentError,
    LLMProviderName,
    LLMTransientError,
    ModelInfo,
    status_error,
)

# Extraction payloads are a few thousand tokens at most; 16k keeps a
# non-streaming request comfortably under the SDK's HTTP timeout while
# never truncating a long resume's JSON.
_MAX_TOKENS = 16000


class AnthropicProvider:
    provider_name = LLMProviderName.ANTHROPIC

    def __init__(self, api_key: str, model: str, timeout_seconds: float, max_retries: int):
        self.model = model
        self.client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def generate_json(self, prompt: str, response_schema: type[BaseModel]) -> dict:
        try:
            # messages.parse() sends the Pydantic schema as structured
            # output (output_config.format) and validates the reply.
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=response_schema,
            )
        except anthropic.APIStatusError as exc:
            raise status_error(exc.status_code, f"Anthropic API error: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"Anthropic connection error: {exc}") from exc
        except ValidationError as exc:
            raise LLMPermanentError(f"Anthropic returned JSON that does not match the schema: {exc}", reason=LLMErrorReason.BAD_OUTPUT) from exc

        if response.stop_reason == "refusal":
            raise LLMPermanentError("Anthropic declined the request (stop_reason=refusal).", reason=LLMErrorReason.REFUSED)
        if response.stop_reason == "max_tokens":
            raise LLMPermanentError("Anthropic response was truncated (stop_reason=max_tokens).", reason=LLMErrorReason.BAD_OUTPUT)
        if response.parsed_output is None:
            raise LLMPermanentError("Anthropic returned no structured output.", reason=LLMErrorReason.BAD_OUTPUT)

        return response.parsed_output.model_dump(mode="json")

    def list_models(self) -> list[ModelInfo]:
        try:
            return [
                ModelInfo(id=m.id, display_name=m.display_name or m.id)
                for m in self.client.models.list()
            ]
        except anthropic.APIStatusError as exc:
            raise status_error(exc.status_code, f"Anthropic API error: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"Anthropic connection error: {exc}") from exc
