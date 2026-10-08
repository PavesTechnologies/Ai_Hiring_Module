"""
LLM capability map + adapter request building (mocked clients - no network):
each adapter sends only the settings its capability entry allows, using its
provider's native structured-output mechanism. Also structured_call's
validation and single retry, identical for every provider.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMErrorReason, LLMPermanentError
from app.services.llm.capabilities import (
    DEFAULT_CAPABILITY_RULES, ModelCapabilities, parse_capability_rules, resolve_capabilities,
)
from app.services.llm.fallback_provider import FallbackLLMProvider
from app.services.llm.gemini_provider import GeminiProvider
from app.services.llm.openai_provider import GroqProvider, OpenAIProvider
from app.services.llm.structured_call import InvalidLLMOutput, structured_call


class Reply(BaseModel):
    answer: str


# --- capability map -------------------------------------------------------

@pytest.mark.parametrize("provider, model, temperature, method, tokens", [
    ("GOOGLE", "gemini-3-flash-preview", False, "response_schema", None),
    ("GOOGLE", "models/gemini-3.5-flash", False, "response_schema", None),
    ("GOOGLE", "gemini-2.5-flash", True, "response_schema", None),
    ("OPENAI", "o4-mini", False, "json_schema", None),
    ("OPENAI", "gpt-5-mini", False, "json_schema", None),
    ("OPENAI", "gpt-4.1-mini", True, "json_schema", None),
    ("ANTHROPIC", "claude-sonnet-4-5", False, "output_format", 16000),   # not sent until verified
    ("GROQ", "openai/gpt-oss-120b", True, "json_object", 16000),
])
def test_default_rules(provider, model, temperature, method, tokens):
    caps = resolve_capabilities(provider, model)
    assert (caps.supports_temperature, caps.structured_output, caps.max_output_tokens) == (temperature, method, tokens)
    assert caps.source


def test_every_default_rule_records_its_source():
    assert all(rule["source"] for rule in DEFAULT_CAPABILITY_RULES)


def test_unverified_rules_never_send_temperature():
    for rule in DEFAULT_CAPABILITY_RULES:
        if any(word in rule["source"].lower() for word in ("verify", "unverified", "not sent")):
            assert rule["supports_temperature"] is False, rule["provider"] + " " + rule["model_pattern"]


def test_configured_rules_override_and_first_match_wins():
    rules = [
        {"provider": "GROQ", "model_pattern": "openai/gpt-oss*", "supports_temperature": False,
         "structured_output": "json_schema", "max_output_tokens": 8000, "source": "test"},
        {"provider": "GROQ", "model_pattern": "*", "supports_temperature": True, "structured_output": "json_object"},
    ]
    assert resolve_capabilities("GROQ", "openai/gpt-oss-20b", rules) == ModelCapabilities(False, "json_schema", 8000, "test")
    assert resolve_capabilities("GROQ", "llama-3.3-70b", rules).structured_output == "json_object"


def test_unsupported_method_falls_back_to_the_adapters_native_one():
    rules = [{"provider": "GOOGLE", "model_pattern": "*", "structured_output": "json_object"}]
    assert resolve_capabilities("GOOGLE", "gemini-2.5-flash", rules).structured_output == "response_schema"


def test_malformed_config_uses_defaults():
    assert parse_capability_rules("not json") is DEFAULT_CAPABILITY_RULES
    assert parse_capability_rules(None) is DEFAULT_CAPABILITY_RULES
    assert parse_capability_rules(json.dumps({"rules": [{"provider": "GROQ"}]})) == [{"provider": "GROQ"}]


# --- adapter request building --------------------------------------------

def _gemini(caps):
    provider = GeminiProvider(api_key="test", model="gemini-x", capabilities=caps)
    provider.client = MagicMock()
    provider.client.models.generate_content.return_value = SimpleNamespace(text='{"answer": "ok"}')
    return provider


@pytest.mark.parametrize("caps, deterministic, expected_extra", [
    (ModelCapabilities(True, "response_schema", None), True, {"temperature": 0}),
    (ModelCapabilities(True, "response_schema", None), False, {}),
    (ModelCapabilities(False, "response_schema", None), True, {}),           # Gemini 3: never sent
    (ModelCapabilities(False, "response_schema", 4096), True, {"max_output_tokens": 4096}),
])
def test_gemini_request(caps, deterministic, expected_extra):
    provider = _gemini(caps)
    assert provider.generate_json("p", Reply, deterministic=deterministic) == {"answer": "ok"}
    config = provider.client.models.generate_content.call_args.kwargs["config"]
    assert config == {"response_mime_type": "application/json", "response_schema": Reply, **expected_extra}


def _openai(caps, cls=OpenAIProvider):
    provider = cls(api_key="test", model="m", timeout_seconds=5, max_retries=0, capabilities=caps)
    provider.client = MagicMock()
    message = SimpleNamespace(refusal=None, parsed=Reply(answer="ok"))
    provider.client.chat.completions.parse.return_value = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    provider.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content='{"answer": "ok"}'))])
    return provider


@pytest.mark.parametrize("caps, deterministic, expected_extra", [
    (ModelCapabilities(True, "json_schema", None), True, {"temperature": 0}),
    (ModelCapabilities(False, "json_schema", None), True, {}),               # reasoning models
    (ModelCapabilities(True, "json_schema", 2000), False, {"max_completion_tokens": 2000}),
])
def test_openai_strict_schema_request(caps, deterministic, expected_extra):
    provider = _openai(caps)
    assert provider.generate_json("p", Reply, deterministic=deterministic) == {"answer": "ok"}
    kwargs = provider.client.chat.completions.parse.call_args.kwargs
    assert kwargs["response_format"] is Reply
    assert {k: v for k, v in kwargs.items() if k in ("temperature", "max_completion_tokens")} == expected_extra
    provider.client.chat.completions.create.assert_not_called()


def test_openai_json_object_mode_when_configured():
    provider = _openai(ModelCapabilities(True, "json_object", None))
    provider.generate_json("p", Reply, deterministic=True)
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"} and kwargs["temperature"] == 0
    provider.client.chat.completions.parse.assert_not_called()


@pytest.mark.parametrize("caps, deterministic, expected_format, expected_extra", [
    (ModelCapabilities(True, "json_object", 16000), True, {"type": "json_object"},
     {"temperature": 0, "max_completion_tokens": 16000}),
    (ModelCapabilities(False, "json_object", None), True, {"type": "json_object"}, {}),
    (ModelCapabilities(True, "json_schema", 8000), False, "json_schema", {"max_completion_tokens": 8000}),
])
def test_groq_request(caps, deterministic, expected_format, expected_extra):
    provider = _openai(caps, GroqProvider)
    assert provider.generate_json("p", Reply, deterministic=deterministic) == {"answer": "ok"}
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    if expected_format == "json_schema":
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["strict"] is False
        assert kwargs["response_format"]["json_schema"]["schema"] == Reply.model_json_schema()
    else:
        assert kwargs["response_format"] == expected_format
    assert {k: v for k, v in kwargs.items() if k in ("temperature", "max_completion_tokens")} == expected_extra
    assert "JSON schema" in kwargs["messages"][0]["content"]          # schema always in the prompt too
    provider.client.chat.completions.parse.assert_not_called()       # never the strict path on Groq


def _anthropic(caps):
    provider = AnthropicProvider(api_key="test", model="claude-x", timeout_seconds=5, max_retries=0, capabilities=caps)
    provider.client = MagicMock()
    provider.client.messages.parse.return_value = SimpleNamespace(stop_reason="end_turn", parsed_output=Reply(answer="ok"))
    return provider


@pytest.mark.parametrize("caps, deterministic, expected", [
    (ModelCapabilities(True, "output_format", 16000), True, {"max_tokens": 16000, "temperature": 0}),
    (ModelCapabilities(True, "output_format", 16000), False, {"max_tokens": 16000}),
    (ModelCapabilities(False, "output_format", None), True, {"max_tokens": 16000}),  # required, default limit
])
def test_anthropic_request(caps, deterministic, expected):
    provider = _anthropic(caps)
    assert provider.generate_json("p", Reply, deterministic=deterministic) == {"answer": "ok"}
    kwargs = provider.client.messages.parse.call_args.kwargs
    assert kwargs["output_format"] is Reply
    assert {k: v for k, v in kwargs.items() if k in ("max_tokens", "temperature")} == expected


def test_fallback_forwards_deterministic_to_the_model_that_answers():
    primary, backup = MagicMock(provider_name="GROQ", model="a"), MagicMock(provider_name="GROQ", model="b")
    primary.generate_json.side_effect = LLMPermanentError("bad", reason=LLMErrorReason.BAD_OUTPUT)
    backup.generate_json.return_value = {"answer": "ok"}
    provider = FallbackLLMProvider(primary, backup, cache=None)
    assert provider.generate_json("p", Reply, deterministic=True) == {"answer": "ok"}
    assert backup.generate_json.call_args.kwargs == {"deterministic": True}
    assert (provider.last_model_used, provider.last_used_fallback) == ("b", True)


# --- structured_call: same validation + one retry for every provider ------

class Seq:
    provider_name, model = "OPENAI", "m"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def generate_json(self, prompt, schema, *, deterministic=False):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_schema_invalid_output_is_retried_once():
    provider = Seq({"wrong": 1}, {"answer": "ok"})
    result = structured_call(provider, "p", Reply)
    assert (result.value.answer, result.attempts, provider.calls) == ("ok", 2, 2)


def test_provider_bad_output_is_retried_once_then_raises():
    bad = LLMPermanentError("x", reason=LLMErrorReason.BAD_OUTPUT)
    provider = Seq(bad, bad)
    with pytest.raises(LLMPermanentError) as exc:
        structured_call(provider, "p", Reply)
    assert exc.value.reason == LLMErrorReason.BAD_OUTPUT and provider.calls == 2


def test_other_errors_are_not_retried():
    provider = Seq(LLMPermanentError("auth", reason=LLMErrorReason.INVALID_KEY))
    with pytest.raises(LLMPermanentError):
        structured_call(provider, "p", Reply)
    assert provider.calls == 1


def test_failed_check_can_return_the_last_reply():
    def check(value):
        raise InvalidLLMOutput("quote not found")

    provider = Seq({"answer": "a"}, {"answer": "b"})
    result = structured_call(provider, "p", Reply, check=check, accept_invalid=True)
    assert (result.value.answer, result.problem, provider.calls) == ("b", "quote not found", 2)
    with pytest.raises(LLMPermanentError):
        structured_call(Seq({"answer": "a"}, {"answer": "b"}), "p", Reply, check=check)
