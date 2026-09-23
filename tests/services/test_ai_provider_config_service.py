"""
AIProviderConfigService - MagicMock style, matching
test_microsoft_oauth_service.py. No provider SDK is ever called: the
provider built by build_provider is patched.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.ai_provider.ai_provider_schema import AIProviderConfigRequest, ListModelsRequest
from app.services.ai_provider_config_service import AIProviderConfigService
from app.services.llm.base import LLMPermanentError, ModelInfo

_BUILD = "app.services.ai_provider_config_service.build_provider"


def _make_service(active=None):
    repo = MagicMock()
    repo.get_active.return_value = active
    repo.upsert_active.side_effect = lambda config: SimpleNamespace(
        **{**config.__dict__, "id": uuid4(), "updated_at": None}
    )
    encryption_service = MagicMock()
    encryption_service.encrypt.side_effect = lambda value, purpose: (f"enc({value})".encode(), uuid4())
    encryption_service.decrypt.side_effect = (
        lambda ciphertext, key_id: ciphertext.decode().removeprefix("enc(").removesuffix(")")
    )
    audit_service = MagicMock()
    return AIProviderConfigService(repo, encryption_service, audit_service), repo, encryption_service, audit_service


def _working_provider():
    provider = MagicMock()
    provider.generate_json.return_value = {"ok": True}
    return provider


def _saved_row(provider="ANTHROPIC", key="sk-ant-secret-1234"):
    return SimpleNamespace(
        id=uuid4(), provider=provider, model_name="claude-opus-5",
        api_key_encrypted=f"enc({key})".encode(), encryption_key_id=uuid4(),
        api_key_last4=key[-4:], is_verified=True, verified_at=None, updated_by="u1", updated_at=None,
    )


def test_verify_passes_when_provider_returns_schema_json():
    service, *_ = _make_service()
    with patch(_BUILD, return_value=_working_provider()):
        result = service.verify(AIProviderConfigRequest(provider="OPENAI", model_name="gpt-5", api_key="sk-x"))
    assert result.verified is True
    assert result.latency_ms is not None


def test_verify_reports_invalid_key_without_raising():
    service, *_ = _make_service()
    failing = MagicMock()
    failing.generate_json.side_effect = LLMPermanentError("401 bad key", status_code=401)
    with patch(_BUILD, return_value=failing):
        result = service.verify(AIProviderConfigRequest(provider="OPENAI", model_name="gpt-5", api_key="sk-x"))
    assert result.verified is False
    assert "API key is invalid" in result.message


def test_save_rejects_a_model_that_fails_verification():
    service, repo, _, audit = _make_service()
    failing = MagicMock()
    failing.generate_json.side_effect = LLMPermanentError("404", status_code=404)
    with patch(_BUILD, return_value=failing), pytest.raises(HTTPException) as err:
        service.save(
            AIProviderConfigRequest(provider="GROQ", model_name="nope", api_key="gsk-x"),
            updated_by="u1", actor_role="HR_ADMIN",
        )
    assert err.value.status_code == 422
    repo.upsert_active.assert_not_called()
    audit.log.assert_not_called()


def test_save_encrypts_the_key_and_never_returns_it():
    service, repo, encryption, audit = _make_service()
    with patch(_BUILD, return_value=_working_provider()):
        saved = service.save(
            AIProviderConfigRequest(provider="ANTHROPIC", model_name="claude-opus-5", api_key="sk-ant-secret-1234"),
            updated_by="u1", actor_role="HR_ADMIN",
        )
    encryption.encrypt.assert_called_once_with("sk-ant-secret-1234", "AI_PROVIDER_KEY")
    stored = repo.upsert_active.call_args[0][0]
    assert stored.api_key_encrypted == b"enc(sk-ant-secret-1234)"
    assert stored.is_verified is True
    repo.commit.assert_called_once()
    assert saved.api_key_masked == "••••1234"
    assert "sk-ant-secret" not in saved.model_dump_json()
    assert "sk-ant-secret" not in str(audit.log.call_args)


def test_blank_key_reuses_the_saved_key_for_the_same_provider():
    service, *_ = _make_service(active=_saved_row(provider="ANTHROPIC"))
    with patch(_BUILD, return_value=_working_provider()) as build:
        service.verify(AIProviderConfigRequest(provider="ANTHROPIC", model_name="claude-sonnet-5", api_key="  "))
    assert build.call_args[0][1] == "sk-ant-secret-1234"


def test_blank_key_is_never_reused_across_providers():
    service, *_ = _make_service(active=_saved_row(provider="ANTHROPIC"))
    with patch(_BUILD) as build, pytest.raises(HTTPException) as err:
        service.verify(AIProviderConfigRequest(provider="OPENAI", model_name="gpt-5"))
    assert err.value.status_code == 422
    build.assert_not_called()


def test_list_models_maps_provider_errors_to_400():
    service, *_ = _make_service()
    failing = MagicMock()
    failing.list_models.side_effect = LLMPermanentError("401", status_code=401)
    with patch(_BUILD, return_value=failing), pytest.raises(HTTPException) as err:
        service.list_models(ListModelsRequest(provider="GOOGLE", api_key="k"))
    assert err.value.status_code == 400


def test_list_models_returns_provider_models():
    service, *_ = _make_service()
    provider = MagicMock()
    provider.list_models.return_value = [ModelInfo(id="gemini-2.5-pro", display_name="Gemini 2.5 Pro")]
    with patch(_BUILD, return_value=provider):
        models = service.list_models(ListModelsRequest(provider="GOOGLE", api_key="k"))
    assert [m.id for m in models] == ["gemini-2.5-pro"]


def test_get_current_reports_env_fallback_when_nothing_saved():
    service, *_ = _make_service(active=None)
    current = service.get_current()
    assert current.source == "env_fallback"
    assert current.provider == "GOOGLE"


def test_reset_deactivates_and_audits():
    row = _saved_row()
    service, repo, _, audit = _make_service(active=row)
    repo.get_active.side_effect = [row, None]
    current = service.reset_to_default(updated_by="u1", actor_role="HR_ADMIN")
    repo.deactivate.assert_called_once_with(row)
    audit.log.assert_called_once()
    repo.commit.assert_called_once()
    assert current.source == "env_fallback"


_RAW_GEMINI_ERROR = "Gemini API error: 400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid.', 'reason': 'API_KEY_INVALID'}}"


def test_list_models_shows_friendly_message_for_gemini_bad_key():
    service, *_ = _make_service()
    failing = MagicMock()
    failing.list_models.side_effect = LLMPermanentError(_RAW_GEMINI_ERROR, status_code=400)
    with patch(_BUILD, return_value=failing), pytest.raises(HTTPException) as err:
        service.list_models(ListModelsRequest(provider="GOOGLE", api_key="bad"))
    assert err.value.detail == "Couldn't load models. The API key is invalid. Check the key and try again."


@pytest.mark.parametrize("error", [
    LLMPermanentError(_RAW_GEMINI_ERROR, status_code=400),
    LLMPermanentError("Groq returned invalid JSON: Expecting value: line 1 column 1"),
    ValueError("No API key was provided. Please pass a valid API key."),
    RuntimeError("httpx.ReadTimeout: something low-level"),
])
def test_check_never_leaks_raw_provider_text(error):
    service, *_ = _make_service()
    failing = MagicMock()
    failing.generate_json.side_effect = error
    with patch(_BUILD, return_value=failing):
        result = service.verify(AIProviderConfigRequest(provider="GOOGLE", model_name="m", api_key="k"))
    assert result.verified is False
    for raw in ("INVALID_ARGUMENT", "{", "Expecting value", "httpx", "Gemini API error"):
        assert raw not in result.message


def test_list_models_handles_non_llm_exceptions():
    service, *_ = _make_service()
    with patch(_BUILD, side_effect=ValueError("No API key was provided.")), pytest.raises(HTTPException) as err:
        service.list_models(ListModelsRequest(provider="GOOGLE", api_key="k"))
    assert err.value.status_code == 400
    assert "No API key was provided" not in err.value.detail


def test_save_db_failure_returns_friendly_503():
    service, repo, *_ = _make_service()
    repo.commit.side_effect = RuntimeError("psycopg2.OperationalError: server closed the connection")
    with patch(_BUILD, return_value=_working_provider()), pytest.raises(HTTPException) as err:
        service.save(
            AIProviderConfigRequest(provider="OPENAI", model_name="gpt-5", api_key="sk-x"),
            updated_by="u1", actor_role="HR_ADMIN",
        )
    assert err.value.status_code == 503
    assert "psycopg2" not in err.value.detail
    repo.rollback.assert_called_once()
