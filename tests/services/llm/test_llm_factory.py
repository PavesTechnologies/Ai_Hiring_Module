from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import FailureClassification
from app.services.document_processing.error_classifier import classify
from app.services.llm import factory
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError, status_error
from app.services.llm.gemini_provider import GeminiProvider
from app.services.llm.openai_provider import GroqProvider, OpenAIProvider

_REPO = "app.services.llm.factory.AIProviderConfigRepository"
_ENC = "app.services.llm.factory.EncryptionService"


@pytest.fixture(autouse=True)
def _env_gemini_key(monkeypatch):
    # genai.Client refuses to build without a key; the dev .env leaves it blank.
    monkeypatch.setattr(factory.settings, "gemini_api_key", "test-gemini-key")


def _row(**overrides):
    values = dict(
        provider="ANTHROPIC", model_name="claude-opus-5", is_verified=True,
        api_key_encrypted=b"cipher", encryption_key_id=uuid4(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_no_saved_config_falls_back_to_env_gemini():
    with patch(_REPO) as repo:
        repo.return_value.get_active.return_value = None
        provider = factory.resolve_active_provider(MagicMock())
    assert isinstance(provider, GeminiProvider)


def test_unverified_config_is_ignored():
    with patch(_REPO) as repo:
        repo.return_value.get_active.return_value = _row(is_verified=False)
        provider = factory.resolve_active_provider(MagicMock())
    assert isinstance(provider, GeminiProvider)


def test_verified_config_builds_the_saved_provider_with_decrypted_key():
    with patch(_REPO) as repo, patch(_ENC) as enc:
        repo.return_value.get_active.return_value = _row()
        enc.return_value.decrypt.return_value = "sk-ant-real"
        provider = factory.resolve_active_provider(MagicMock())
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-opus-5"


def test_undecryptable_key_falls_back_to_env_gemini():
    with patch(_REPO) as repo, patch(_ENC) as enc:
        repo.return_value.get_active.return_value = _row()
        enc.return_value.decrypt.side_effect = RuntimeError("missing key material")
        provider = factory.resolve_active_provider(MagicMock())
    assert isinstance(provider, GeminiProvider)


@pytest.mark.parametrize("name,cls", [
    ("GOOGLE", GeminiProvider), ("ANTHROPIC", AnthropicProvider),
    ("OPENAI", OpenAIProvider), ("GROQ", GroqProvider),
])
def test_build_provider_maps_every_provider(name, cls):
    assert isinstance(factory.build_provider(name, "key", "model"), cls)


def test_build_provider_rejects_unknown_provider():
    with pytest.raises(LLMPermanentError):
        factory.build_provider("MISTRAL", "key", "model")


def test_groq_uses_groq_base_url():
    provider = factory.build_provider("GROQ", "key", "llama")
    assert "api.groq.com" in str(provider.client.base_url)


@pytest.mark.parametrize("status,message,expected", [
    (500, "overloaded", LLMTransientError),
    (529, "overloaded", LLMTransientError),
    (None, "no response", LLMTransientError),
    (429, "rate limit reached", LLMTransientError),
    (429, "You exceeded your current quota", LLMPermanentError),
    (429, "insufficient_quota", LLMPermanentError),
    (401, "invalid key", LLMPermanentError),
    (404, "model not found", LLMPermanentError),
])
def test_status_error_mapping(status, message, expected):
    error = status_error(status, message)
    assert type(error) is expected
    assert error.status_code == status


def test_classifier_maps_neutral_llm_errors():
    assert classify(LLMTransientError("x")) is FailureClassification.TRANSIENT
    assert classify(LLMPermanentError("x")) is FailureClassification.PERMANENT


_GEMINI_BAD_KEY = (
    "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'API key not valid. Please pass a valid "
    "API key.', 'status': 'INVALID_ARGUMENT', 'details': [{'reason': 'API_KEY_INVALID'}]}}"
)


@pytest.mark.parametrize("status,message,expected", [
    (400, _GEMINI_BAD_KEY, LLMErrorReason.INVALID_KEY),                          # Gemini: bad key is a 400
    (401, "invalid x-api-key", LLMErrorReason.INVALID_KEY),                       # Anthropic
    (401, "Incorrect API key provided", LLMErrorReason.INVALID_KEY),              # OpenAI
    (401, "Invalid API Key", LLMErrorReason.INVALID_KEY),                         # Groq
    (403, "permission denied", LLMErrorReason.NO_ACCESS),
    (404, "The model `gpt-9` does not exist", LLMErrorReason.MODEL_NOT_FOUND),
    (400, "models/foo is not found for API version v1beta", LLMErrorReason.MODEL_NOT_FOUND),
    (429, "You exceeded your current quota", LLMErrorReason.QUOTA_EXHAUSTED),
    (429, "Rate limit reached", LLMErrorReason.RATE_LIMITED),
    (503, "overloaded", LLMErrorReason.UNAVAILABLE),
    (None, "connection reset", LLMErrorReason.UNAVAILABLE),
    (400, "unsupported parameter", LLMErrorReason.BAD_REQUEST),
])
def test_error_reason_detection(status, message, expected):
    assert status_error(status, message).reason == expected
