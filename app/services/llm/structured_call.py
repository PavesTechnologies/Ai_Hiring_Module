"""
One structured LLM call with the same post-checks for every provider:
the adapter uses its provider's native JSON mechanism (see
capabilities.py), then the reply is validated against the Pydantic schema
and by the caller's own check (e.g. "every quote appears in the text").
Invalid output - the provider's BAD_OUTPUT, a schema mismatch, or a failed
caller check - is retried once; a second failure raises.

The result carries which provider/model answered (and whether that was the
backup model), so callers can store it with what they derive from it.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 2


class InvalidLLMOutput(ValueError):
    """Raised by a caller's check when a schema-valid reply is still unusable."""


@dataclass(frozen=True)
class LLMCallInfo:
    provider: str | None
    model: str | None
    used_fallback: bool = False

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "used_fallback": self.used_fallback}


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    value: T
    call: LLMCallInfo
    attempts: int
    # Set only with accept_invalid=True: why the last (returned) reply
    # still failed the caller's check after the retry.
    problem: str | None = None


def call_info(provider: LLMProvider) -> LLMCallInfo:
    """Which model answered the provider's last call - the backup model when FallbackLLMProvider used it."""
    used_fallback = bool(getattr(provider, "last_used_fallback", False))
    model = getattr(provider, "last_model_used", None) or getattr(provider, "model", None)
    return LLMCallInfo(getattr(provider, "provider_name", None), model, used_fallback)


def structured_call(
    provider: LLMProvider,
    prompt: str,
    schema: type[T],
    *,
    check: Callable[[T], None] | None = None,
    deterministic: bool = False,
    accept_invalid: bool = False,
) -> StructuredResult[T]:
    """
    accept_invalid: when the reply is schema-valid but still fails `check`
    after the retry, return it (with `problem` set) instead of raising - for
    callers that can use the parts that pass (e.g. drop only the items whose
    quote isn't found). Unparseable/schema-invalid output always raises.
    """
    last_error: Exception | None = None
    last_checked: StructuredResult | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = provider.generate_json(prompt, schema, deterministic=deterministic)
            value = schema.model_validate(raw)
            if check is not None:
                try:
                    check(value)
                except InvalidLLMOutput as exc:
                    last_checked = StructuredResult(value, call_info(provider), attempt, problem=str(exc))
                    raise
            return StructuredResult(value=value, call=call_info(provider), attempts=attempt)
        except LLMPermanentError as exc:
            if exc.reason != LLMErrorReason.BAD_OUTPUT:
                raise
            last_error = exc
        except (ValidationError, InvalidLLMOutput) as exc:
            last_error = exc
        logger.warning("LLM structured output invalid (attempt %s/%s, schema=%s): %s",
                       attempt, MAX_ATTEMPTS, schema.__name__, str(last_error)[:300])
    if accept_invalid and last_checked is not None and isinstance(last_error, InvalidLLMOutput):
        return last_checked
    raise LLMPermanentError(
        f"LLM output still invalid after {MAX_ATTEMPTS} attempts: {last_error}",
        reason=LLMErrorReason.BAD_OUTPUT,
    ) from last_error
