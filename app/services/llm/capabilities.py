"""
Per-model LLM capabilities - which request settings a model accepts, so
adapters send only what it supports and no service hardcodes provider
quirks.

The map lives in platform_config (LLM_MODEL_CAPABILITIES, a JSON list of
rules). The first rule whose provider matches and whose model_pattern
(fnmatch, case-insensitive) matches the model id wins - so specific
patterns go before the provider's "*" catch-all. DEFAULT_CAPABILITY_RULES
below is the seeded value, used as-is when the config row is missing or
unreadable; each rule records where its values came from in `source`.

Fields:
  supports_temperature  the model accepts an explicit temperature. When a
                        caller asks for a deterministic answer, the
                        adapter sends temperature 0 only if this is true.
                        Policy: false until verified for that model.
  structured_output     the native JSON mechanism the adapter uses:
                          GOOGLE    "response_schema"
                          OPENAI    "json_schema" (strict, via parse())
                          ANTHROPIC "output_format" (via messages.parse())
                          GROQ      "json_object" (schema in the prompt)
                                    or "json_schema" (non-strict)
  max_output_tokens     sent as the provider's output-token limit; null
                        means "don't send one" (provider default).
"""
import fnmatch
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LLM_MODEL_CAPABILITIES_KEY = "LLM_MODEL_CAPABILITIES"

# Structured-output methods each adapter implements.
STRUCTURED_OUTPUT_METHODS = {
    "GOOGLE": ("response_schema",),
    "OPENAI": ("json_schema", "json_object"),
    "ANTHROPIC": ("output_format",),
    "GROQ": ("json_object", "json_schema"),
}


@dataclass(frozen=True)
class ModelCapabilities:
    supports_temperature: bool
    structured_output: str
    max_output_tokens: int | None = None
    source: str = ""


DEFAULT_CAPABILITY_RULES: list[dict] = [
    {
        "provider": "GOOGLE", "model_pattern": "gemini-3*",
        "supports_temperature": False, "structured_output": "response_schema", "max_output_tokens": None,
        "source": "Google Gemini 3 developer guide: keep temperature at its default (lowering it can cause "
                  "looping or degraded output). Structured output: current adapter (response_schema).",
    },
    {
        "provider": "GOOGLE", "model_pattern": "*",
        "supports_temperature": True, "structured_output": "response_schema", "max_output_tokens": None,
        "source": "Gemini API GenerationConfig.temperature (Gemini 2.x and earlier). Structured output: "
                  "current adapter (response_schema). No output limit sent today.",
    },
    {
        "provider": "OPENAI", "model_pattern": "o[0-9]*",
        "supports_temperature": False, "structured_output": "json_schema", "max_output_tokens": None,
        "source": "OpenAI reasoning models (o-series) accept only the default temperature. Structured "
                  "output: current adapter (strict json_schema via parse()).",
    },
    {
        "provider": "OPENAI", "model_pattern": "gpt-5*",
        "supports_temperature": False, "structured_output": "json_schema", "max_output_tokens": None,
        "source": "OpenAI GPT-5 models: temperature NOT sent - reasoning variants accept only the default, "
                  "and non-reasoning chat variants are unverified. Structured output: current adapter.",
    },
    {
        "provider": "OPENAI", "model_pattern": "*",
        "supports_temperature": True, "structured_output": "json_schema", "max_output_tokens": None,
        "source": "OpenAI Chat Completions temperature (gpt-4o / gpt-4.1 family). Structured output: "
                  "current adapter. No output limit sent today.",
    },
    {
        "provider": "ANTHROPIC", "model_pattern": "*",
        "supports_temperature": False, "structured_output": "output_format", "max_output_tokens": 16000,
        "source": "Temperature NOT sent until verified per model (newer Claude models may restrict "
                  "sampling parameters). Structured output and 16000 max_tokens: current adapter values.",
    },
    {
        "provider": "GROQ", "model_pattern": "*",
        "supports_temperature": True, "structured_output": "json_object", "max_output_tokens": 16000,
        "source": "Groq OpenAI-compatible API temperature. json_object mode and 16000 "
                  "max_completion_tokens: current adapter values (GROQ_MAX_COMPLETION_TOKENS).",
    },
]

# Used only if no rule matches at all (unknown provider) - the most
# conservative request: no temperature, no limit.
_FALLBACK = ModelCapabilities(supports_temperature=False, structured_output="", source="no matching rule")


def parse_capability_rules(raw: str | None) -> list[dict]:
    """The configured rules, or the defaults when the value is missing or malformed."""
    if not raw:
        return DEFAULT_CAPABILITY_RULES
    try:
        rules = json.loads(raw)
        if isinstance(rules, dict):
            rules = rules.get("rules")
        if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
            raise ValueError("expected a list of rule objects")
        return rules
    except (ValueError, TypeError):
        logger.error("%s is not valid JSON rules - using the built-in defaults.", LLM_MODEL_CAPABILITIES_KEY)
        return DEFAULT_CAPABILITY_RULES


def load_capability_rules(db=None) -> list[dict]:
    if db is None:
        return DEFAULT_CAPABILITY_RULES
    try:
        from app.repositories.config_repository import ConfigRepository
        raw = ConfigRepository(db).get_configs_by_keys([LLM_MODEL_CAPABILITIES_KEY]).get(LLM_MODEL_CAPABILITIES_KEY)
    except Exception:
        logger.exception("Could not read %s - using the built-in defaults.", LLM_MODEL_CAPABILITIES_KEY)
        return DEFAULT_CAPABILITY_RULES
    return parse_capability_rules(raw)


def resolve_capabilities(provider: str, model: str, rules: list[dict] | None = None) -> ModelCapabilities:
    model_id = (model or "").lower().removeprefix("models/")
    for rule in rules if rules is not None else DEFAULT_CAPABILITY_RULES:
        if str(rule.get("provider", "")).upper() != (provider or "").upper():
            continue
        if not fnmatch.fnmatchcase(model_id, str(rule.get("model_pattern", "*")).lower()):
            continue
        method = rule.get("structured_output") or STRUCTURED_OUTPUT_METHODS.get(provider.upper(), ("",))[0]
        if method not in STRUCTURED_OUTPUT_METHODS.get(provider.upper(), ()):
            logger.error("Capability rule for %s/%s names unsupported structured_output %r - using %r.",
                         provider, model, method, STRUCTURED_OUTPUT_METHODS[provider.upper()][0])
            method = STRUCTURED_OUTPUT_METHODS[provider.upper()][0]
        max_tokens = rule.get("max_output_tokens")
        return ModelCapabilities(
            supports_temperature=bool(rule.get("supports_temperature")),
            structured_output=method,
            max_output_tokens=int(max_tokens) if max_tokens else None,
            source=str(rule.get("source") or ""),
        )
    methods = STRUCTURED_OUTPUT_METHODS.get((provider or "").upper())
    return ModelCapabilities(False, methods[0], None, _FALLBACK.source) if methods else _FALLBACK
