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


# Real Gemini free-tier reply: worded as "exceeded your current quota ...
# billing", but it's a per-minute limit that clears in seconds.
_GEMINI_PER_MINUTE = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, "
    "please check your plan and billing details. * Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 5, model: gemini-3.8-flash"
    "\nPlease retry in 8.394106022s.', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}}"
)
_GEMINI_PER_DAY = (
    "429 RESOURCE_EXHAUSTED. {'error': {'message': 'You exceeded your current quota, please check your plan "
    "and billing details.', 'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}"
)


def test_gemini_per_minute_limit_is_rate_limiting_and_retried():
    error = status_error(429, _GEMINI_PER_MINUTE)
    assert error.reason == LLMErrorReason.RATE_LIMITED
    assert isinstance(error, LLMTransientError)
    assert classify(error) is FailureClassification.TRANSIENT


def test_gemini_per_day_limit_is_daily_limit_and_not_retried():
    error = status_error(429, _GEMINI_PER_DAY)
    assert error.reason == LLMErrorReason.DAILY_LIMIT
    assert isinstance(error, LLMPermanentError)
    assert classify(error) is FailureClassification.PERMANENT


def test_gemini_per_minute_details_are_extracted():
    error = status_error(429, _GEMINI_PER_MINUTE)
    assert error.retry_after_seconds == pytest.approx(8.394106022)
    assert error.quota_limit == 5


@pytest.mark.parametrize("status,message,expected", [
    (429, "You exceeded your current quota. code: insufficient_quota", LLMErrorReason.CREDITS_EXHAUSTED),  # OpenAI
    (400, "Your credit balance is too low to access the Anthropic API.", LLMErrorReason.CREDITS_EXHAUSTED),  # Anthropic
    (429, "Rate limit reached on tokens per minute (TPM). Please try again in 2.5s.", LLMErrorReason.RATE_LIMITED),  # Groq/OpenAI
    (429, "Rate limit reached on requests per day (RPD).", LLMErrorReason.DAILY_LIMIT),  # Groq
    (429, "RESOURCE_EXHAUSTED", LLMErrorReason.QUOTA_EXHAUSTED),  # no window stated
])
def test_quota_errors_get_a_specific_code(status, message, expected):
    assert status_error(status, message).reason == expected


# ── Model lists only offer models AIRS can actually use ───────────────────

from app.services.llm.gemini_provider import _is_usable_gemini_model  # noqa: E402


@pytest.mark.parametrize("model_id,display,usable", [
    ("gemini-3.8-flash", "Gemini 3.8 Flash", True),
    ("gemini-2.5-pro", "Gemini 2.5 Pro", True),
    ("gemini-2.5-flash-preview-tts", "Gemini 2.5 Flash Preview TTS", False),
    ("gemini-2.5-flash-image", "Nano Banana", False),
    ("gemini-live-2.5-flash", "Gemini Live", False),
    ("gemini-2.5-flash-native-audio", "Native Audio", False),
    ("gemma-3-27b-it", "Gemma 3 27B", False),
    ("gemini-robotics-er-1.5-preview", "Robotics", False),
    ("gemini-1.5-pro", "Gemini 1.5 Pro (deprecated)", False),
])
def test_gemini_model_filter(model_id, display, usable):
    assert _is_usable_gemini_model(model_id, ["generateContent"], display) is usable


def test_gemini_models_without_generate_content_are_hidden():
    assert _is_usable_gemini_model("text-embedding-004", ["embedContent"], "Embedding") is False


@pytest.mark.parametrize("model_id,usable", [
    ("gpt-5", True), ("gpt-5-mini", True), ("o4-mini", True), ("gpt-4.1", True),
    ("gpt-5-pro", False), ("o3-pro", False), ("gpt-5-codex", False), ("o3-deep-research", False),
    ("gpt-3.5-turbo-instruct", False), ("gpt-4o-audio-preview", False), ("gpt-4o-realtime-preview", False),
    ("gpt-image-1", False), ("text-embedding-3-large", False), ("omni-moderation-latest", False),
    ("dall-e-3", False), ("whisper-1", False),
])
def test_openai_model_filter(model_id, usable):
    assert OpenAIProvider._is_usable(SimpleNamespace(id=model_id)) is usable


@pytest.mark.parametrize("model,usable", [
    (SimpleNamespace(id="openai/gpt-oss-120b", active=True, context_window=131072), True),
    (SimpleNamespace(id="qwen/qwen3.8-27b", active=True, context_window=131072), True),
    (SimpleNamespace(id="allam-2-7b", active=True, context_window=4096), False),          # window too small
    (SimpleNamespace(id="llama-old", active=False, context_window=131072), False),        # inactive
    (SimpleNamespace(id="whisper-large-v3", active=True, context_window=448), False),
    (SimpleNamespace(id="canopylabs/orpheus-v1-english", active=True, context_window=4000), False),
    (SimpleNamespace(id="openai/gpt-oss-safeguard-20b", active=True, context_window=131072), False),
    (SimpleNamespace(id="meta-llama/llama-prompt-guard-2-86m", active=True, context_window=512), False),
])
def test_groq_model_filter(model, usable):
    assert GroqProvider._is_usable(model) is usable
