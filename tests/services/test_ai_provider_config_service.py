"""
AIProviderConfigService - MagicMock style, matching
test_microsoft_oauth_service.py. No provider SDK is ever called: the
provider built by build_provider is patched. Covers the multi-provider
rules (one row per provider, one active, last/active can't be deleted) and
that every failure reaches the user as plain English.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.schemas.ai_provider.ai_provider_schema import (
    CreateAIProviderRequest,
    ListModelsRequest,
    UpdateAIProviderRequest,
    VerifyRequest,
)
from app.services.ai_provider_config_service import AIProviderConfigService
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError, ModelInfo

_BUILD = "app.services.ai_provider_config_service.build_provider"
_RAW_GEMINI_ERROR = (
    "Gemini API error: 400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid.', "
    "'reason': 'API_KEY_INVALID'}}"
)
# Fragments that only appear in raw SDK/DB errors - none may reach a user.
_RAW_MARKERS = ("INVALID_ARGUMENT", "{", "Traceback", "psycopg2", "httpx", "Gemini API error", "Expecting value")


def _row(provider="ANTHROPIC", key="sk-ant-secret-1234", *, is_active=False, is_verified=True, model="claude-opus-5"):
    return SimpleNamespace(
        id=uuid4(), provider=provider, model_name=model,
        api_key_encrypted=f"enc({key})".encode(), encryption_key_id=uuid4(),
        api_key_last4=key[-4:], is_active=is_active, is_verified=is_verified,
        verified_at=None, last_error=None, updated_by="u1", updated_at=None,
    )


def _make_service(rows=()):
    """Repository mock backed by an in-memory list, so the rules see real state."""
    rows = list(rows)
    repo = MagicMock()
    repo.get_active.side_effect = lambda: next((r for r in rows if r.is_active), None)
    repo.get_by_id.side_effect = lambda config_id: next((r for r in rows if r.id == config_id), None)
    repo.get_by_provider.side_effect = lambda p: next((r for r in rows if r.provider == p), None)
    repo.count.side_effect = lambda: len(rows)
    repo.registered_providers.side_effect = lambda: {r.provider for r in rows}
    repo.list.side_effect = lambda page, page_size: rows[(page - 1) * page_size: page * page_size]

    def _create(config):
        created = SimpleNamespace(**{k: v for k, v in vars(config).items() if not k.startswith("_")})
        created.id, created.updated_at = uuid4(), None
        rows.append(created)
        return created

    def _set_active(config):
        for r in rows:
            r.is_active = r is config
        return config

    repo.create.side_effect = _create
    repo.update.side_effect = lambda config: config
    repo.set_active.side_effect = _set_active
    repo.delete.side_effect = lambda config: rows.remove(config)

    encryption = MagicMock()
    encryption.encrypt.side_effect = lambda value, purpose: (f"enc({value})".encode(), uuid4())
    encryption.decrypt.side_effect = lambda ciphertext, key_id: ciphertext.decode()[4:-1]
    audit = MagicMock()
    return AIProviderConfigService(repo, encryption, audit), repo, encryption, audit, rows


def _working():
    provider = MagicMock()
    provider.generate_json.return_value = {"ok": True}
    return provider


def _failing(error):
    provider = MagicMock()
    provider.generate_json.side_effect = error
    provider.list_models.side_effect = error
    return provider


def _assert_friendly(message: str):
    assert message
    for raw in _RAW_MARKERS:
        assert raw not in message, f"raw error text {raw!r} leaked into: {message}"


def _create_request(provider="ANTHROPIC", model="claude-opus-5", key="sk-ant-secret-1234"):
    return CreateAIProviderRequest(provider=provider, model_name=model, api_key=key)


# ── Register ──────────────────────────────────────────────────────────────

def test_first_registered_provider_becomes_active():
    service, repo, *_ , rows = _make_service()
    with patch(_BUILD, return_value=_working()):
        row = service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert row.is_active is True
    repo.commit.assert_called_once()


def test_second_provider_is_registered_inactive():
    service, *_, rows = _make_service([_row("GOOGLE", "AIza-1111", is_active=True)])
    with patch(_BUILD, return_value=_working()):
        row = service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert row.is_active is False
    assert [r.provider for r in rows if r.is_active] == ["GOOGLE"]


def test_registering_the_same_provider_twice_is_a_friendly_409():
    service, repo, *_ = _make_service([_row("ANTHROPIC", is_active=True)])
    with patch(_BUILD) as build, pytest.raises(HTTPException) as err:
        service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 409
    assert err.value.detail == "Anthropic Claude is already registered. Edit it instead."
    build.assert_not_called()
    repo.create.assert_not_called()


def test_concurrent_duplicate_registration_is_a_friendly_409():
    service, repo, *_ = _make_service()
    repo.commit.side_effect = IntegrityError("INSERT ...", {}, Exception("duplicate key value violates unique constraint"))
    with patch(_BUILD, return_value=_working()), pytest.raises(HTTPException) as err:
        service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 409
    _assert_friendly(err.value.detail)
    repo.rollback.assert_called_once()


def test_register_is_blocked_when_check_fails_with_friendly_reason():
    service, repo, *_ = _make_service()
    with patch(_BUILD, return_value=_failing(LLMPermanentError(_RAW_GEMINI_ERROR, status_code=400))), \
            pytest.raises(HTTPException) as err:
        service.create(_create_request("GOOGLE", "gemini-2.5-pro", "bad"), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 422
    assert err.value.detail == "The API key is invalid. Check the key and try again."
    repo.create.assert_not_called()


def test_register_encrypts_key_and_never_exposes_it():
    service, repo, encryption, audit, rows = _make_service()
    with patch(_BUILD, return_value=_working()):
        row = service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    encryption.encrypt.assert_called_once_with("sk-ant-secret-1234", "AI_PROVIDER_KEY")
    assert rows[0].api_key_encrypted == b"enc(sk-ant-secret-1234)"
    assert row.api_key_masked == "••••1234"
    assert "sk-ant-secret" not in row.model_dump_json()
    assert "sk-ant-secret" not in str(audit.log.call_args)


def test_register_db_failure_is_a_friendly_503():
    service, repo, *_ = _make_service()
    repo.commit.side_effect = RuntimeError("psycopg2.OperationalError: server closed the connection")
    with patch(_BUILD, return_value=_working()), pytest.raises(HTTPException) as err:
        service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 503
    _assert_friendly(err.value.detail)
    repo.rollback.assert_called_once()


def test_register_without_encryption_setup_is_a_friendly_503():
    service, _, encryption, *_ = _make_service()
    encryption.encrypt.side_effect = RuntimeError("No ACTIVE or ROTATING encryption key for AI_PROVIDER_KEY")
    with patch(_BUILD, return_value=_working()), pytest.raises(HTTPException) as err:
        service.create(_create_request(), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 503
    assert "ENCRYPTION_KEY" not in err.value.detail
    assert "system administrator" in err.value.detail


# ── Edit ──────────────────────────────────────────────────────────────────

def test_edit_with_blank_key_reuses_saved_key_and_rechecks():
    existing = _row("ANTHROPIC", is_active=True)
    service, _, encryption, *_ = _make_service([existing])
    with patch(_BUILD, return_value=_working()) as build:
        row = service.update(existing.id, UpdateAIProviderRequest(model_name="claude-sonnet-5", api_key=""),
                             updated_by="u1", actor_role="HR_ADMIN")
    assert build.call_args[0][:3] == ("ANTHROPIC", "sk-ant-secret-1234", "claude-sonnet-5")
    encryption.encrypt.assert_not_called()
    assert row.model_name == "claude-sonnet-5"
    assert row.api_key_masked == "••••1234"


def test_edit_with_new_key_replaces_it():
    existing = _row("OPENAI", "sk-old-0000")
    service, *_ = _make_service([existing])
    with patch(_BUILD, return_value=_working()):
        row = service.update(existing.id, UpdateAIProviderRequest(model_name="gpt-5", api_key="sk-new-9999"),
                             updated_by="u1", actor_role="HR_ADMIN")
    assert existing.api_key_encrypted == b"enc(sk-new-9999)"
    assert row.api_key_masked == "••••9999"


def test_edit_is_blocked_when_check_fails():
    existing = _row("OPENAI", model="gpt-5")
    service, repo, *_ = _make_service([existing])
    with patch(_BUILD, return_value=_failing(LLMPermanentError("model_not_found", status_code=404))), \
            pytest.raises(HTTPException) as err:
        service.update(existing.id, UpdateAIProviderRequest(model_name="gpt-9"), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 422
    assert err.value.detail == "This model isn't available for this API key. Pick another model."
    assert existing.model_name == "gpt-5"
    repo.commit.assert_not_called()


def test_edit_missing_row_is_a_friendly_404():
    service, *_ = _make_service()
    with pytest.raises(HTTPException) as err:
        service.update(uuid4(), UpdateAIProviderRequest(model_name="m"), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 404
    assert "Refresh the page" in err.value.detail


def test_edit_when_saved_key_cannot_be_decrypted_is_friendly():
    existing = _row("GROQ")
    service, _, encryption, *_ = _make_service([existing])
    encryption.decrypt.side_effect = RuntimeError("InvalidToken")
    with pytest.raises(HTTPException) as err:
        service.update(existing.id, UpdateAIProviderRequest(model_name="llama"), updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 503
    assert err.value.detail == "The saved API key couldn't be read. Please enter the API key again."


# ── Activate ──────────────────────────────────────────────────────────────

def test_activate_switches_the_single_active_provider():
    google, anthropic = _row("GOOGLE", "AIza-1111", is_active=True), _row("ANTHROPIC")
    service, _, _, audit, rows = _make_service([google, anthropic])
    row = service.activate(anthropic.id, updated_by="u1", actor_role="HR_ADMIN")
    assert row.is_active is True
    assert [r.provider for r in rows if r.is_active] == ["ANTHROPIC"]
    audit.log.assert_called_once()


def test_activating_the_active_provider_is_a_no_op():
    google = _row("GOOGLE", is_active=True)
    service, repo, *_ = _make_service([google])
    service.activate(google.id, updated_by="u1", actor_role="HR_ADMIN")
    repo.set_active.assert_not_called()


def test_unverified_provider_cannot_be_activated():
    service, *_ = _make_service([_row("GOOGLE", is_active=True), unverified := _row("GROQ", is_verified=False)])
    with pytest.raises(HTTPException) as err:
        service.activate(unverified.id, updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 409
    _assert_friendly(err.value.detail)


# ── Delete ────────────────────────────────────────────────────────────────

def test_last_provider_cannot_be_deleted():
    only = _row("GOOGLE", is_active=True)
    service, repo, *_ = _make_service([only])
    with pytest.raises(HTTPException) as err:
        service.delete(only.id, updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 409
    assert err.value.detail == "At least one AI provider must remain, so the last one can't be deleted."
    repo.delete.assert_not_called()


def test_active_provider_cannot_be_deleted():
    active, other = _row("GOOGLE", is_active=True), _row("ANTHROPIC")
    service, repo, *_ = _make_service([active, other])
    with pytest.raises(HTTPException) as err:
        service.delete(active.id, updated_by="u1", actor_role="HR_ADMIN")
    assert err.value.status_code == 409
    assert err.value.detail == "This is the active provider. Set another provider as active first."
    repo.delete.assert_not_called()


def test_inactive_provider_can_be_deleted():
    active, other = _row("GOOGLE", is_active=True), _row("ANTHROPIC")
    service, repo, _, audit, rows = _make_service([active, other])
    service.delete(other.id, updated_by="u1", actor_role="HR_ADMIN")
    assert [r.provider for r in rows] == ["GOOGLE"]
    audit.log.assert_called_once()
    repo.commit.assert_called_once()


# ── Reads ─────────────────────────────────────────────────────────────────

def test_options_flag_registered_providers():
    service, *_ = _make_service([_row("GOOGLE", is_active=True)])
    options = {o.key: o.registered for o in service.list_provider_options()}
    assert options == {"GOOGLE": True, "ANTHROPIC": False, "OPENAI": False, "GROQ": False}


def test_list_is_paged_and_masks_keys():
    service, *_ = _make_service([_row(p, f"key-{i}{i}{i}{i}") for i, p in enumerate(["GOOGLE", "ANTHROPIC", "OPENAI"])])
    page = service.list_registered(page=2, page_size=2)
    assert page.total == 3 and len(page.items) == 1
    assert page.items[0].api_key_masked == "••••2222"


def test_active_provider_falls_back_to_env_when_none_registered():
    service, *_ = _make_service()
    assert service.get_active_provider().source == "env_fallback"


def test_active_provider_reports_the_active_row():
    service, *_ = _make_service([_row("OPENAI", is_active=True, model="gpt-5")])
    active = service.get_active_provider()
    assert (active.source, active.provider, active.model_name) == ("database", "OPENAI", "gpt-5")


# ── Check / model list: blank key reuse and friendly errors ───────────────

def test_blank_key_reuses_that_providers_saved_key():
    service, *_ = _make_service([_row("ANTHROPIC")])
    with patch(_BUILD, return_value=_working()) as build:
        service.verify(VerifyRequest(provider="ANTHROPIC", model_name="claude-sonnet-5", api_key="  "))
    assert build.call_args[0][1] == "sk-ant-secret-1234"


def test_blank_key_is_never_reused_across_providers():
    service, *_ = _make_service([_row("ANTHROPIC")])
    with patch(_BUILD) as build, pytest.raises(HTTPException) as err:
        service.verify(VerifyRequest(provider="OPENAI", model_name="gpt-5"))
    assert err.value.status_code == 422
    assert err.value.detail == "Enter an API key for this provider."
    build.assert_not_called()


@pytest.mark.parametrize("error,expected", [
    (LLMPermanentError(_RAW_GEMINI_ERROR, status_code=400), "The API key is invalid. Check the key and try again."),
    (LLMPermanentError("permission denied", status_code=403), "This API key doesn't have permission to use this provider or model."),
    (LLMPermanentError("insufficient_quota", status_code=429), "This API key has run out of quota or credits. Check the billing on the provider's account."),
    (LLMTransientError("Rate limit reached", status_code=429), "The provider is limiting how often this key can be used (free-tier keys allow only a few requests per minute). Wait a minute and try again."),
    (LLMTransientError("upstream connect error", status_code=503), "The provider couldn't be reached or is busy. Try again in a moment."),
    (LLMPermanentError("Groq returned invalid JSON: Expecting value", reason=LLMErrorReason.BAD_OUTPUT), "This model couldn't return the structured results AIRS needs. Pick another model."),
    (LLMPermanentError("declined", reason=LLMErrorReason.REFUSED), "This model declined the test request. Pick another model."),
    (ValueError("No API key was provided."), "Something went wrong while contacting the provider. Try again, or pick another model."),
    (RuntimeError("httpx.ReadTimeout"), "Something went wrong while contacting the provider. Try again, or pick another model."),
])
def test_check_failures_are_plain_english(error, expected):
    service, *_ = _make_service()
    with patch(_BUILD, return_value=_failing(error)):
        result = service.verify(VerifyRequest(provider="GOOGLE", model_name="m", api_key="k"))
    assert result.verified is False
    assert result.message == expected
    _assert_friendly(result.message)


def test_model_list_failure_is_friendly_400():
    service, *_ = _make_service()
    with patch(_BUILD, return_value=_failing(LLMPermanentError(_RAW_GEMINI_ERROR, status_code=400))), \
            pytest.raises(HTTPException) as err:
        service.list_models(ListModelsRequest(provider="GOOGLE", api_key="bad"))
    assert err.value.status_code == 400
    assert err.value.detail == "Couldn't load models. The API key is invalid. Check the key and try again."


def test_model_list_client_construction_error_is_friendly():
    service, *_ = _make_service()
    with patch(_BUILD, side_effect=ValueError("No API key was provided.")), pytest.raises(HTTPException) as err:
        service.list_models(ListModelsRequest(provider="GOOGLE", api_key="k"))
    assert err.value.status_code == 400
    _assert_friendly(err.value.detail)


def test_model_list_returns_models():
    service, *_ = _make_service()
    provider = MagicMock()
    provider.list_models.return_value = [ModelInfo(id="gemini-2.5-pro", display_name="Gemini 2.5 Pro")]
    with patch(_BUILD, return_value=provider):
        models = service.list_models(ListModelsRequest(provider="GOOGLE", api_key="k"))
    assert [m.id for m in models] == ["gemini-2.5-pro"]


# ── Request validation ────────────────────────────────────────────────────

def test_register_requires_a_key():
    with pytest.raises(ValidationError, match="Enter an API key"):
        CreateAIProviderRequest(provider="GOOGLE", model_name="m", api_key="   ")


def test_model_is_required():
    with pytest.raises(ValidationError, match="Select a model"):
        VerifyRequest(provider="GOOGLE", model_name="   ", api_key="k")


def test_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        CreateAIProviderRequest(provider="MISTRAL", model_name="m", api_key="k")
