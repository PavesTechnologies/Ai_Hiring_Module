"""
No secret setting is ever shown: not in repr/str, dumps, the database URL, attribute errors or
validation errors. Each secret gets a unique sentinel value; every rendering is searched for them.
"""
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

SECRET_FIELDS = (
    "db_password", "redis_password", "aws_access_key_id", "aws_secret_access_key",
    "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY", "gemini_api_key", "candidate_pii_key",
    "microsoft_client_secret", "google_client_secret", "oauth_state_signing_key", "feedback_token_signing_key",
)
SENTINELS = {name: f"SENTINEL{i:02d}x{name[-4:]}" for i, name in enumerate(SECRET_FIELDS)}
ENCRYPTION_KEYS = {"ENCRYPTION_KEY_CANDIDATE_PII_V1": "SENTINELenc1", "ENCRYPTION_KEY_AI_PROVIDER_V1": "SENTINELenc2",
                   "ENCRYPTION_KEY_OAUTH_TOKEN_V1": "SENTINELenc3"}


@pytest.fixture
def encryption_keys(monkeypatch):
    for name, value in ENCRYPTION_KEYS.items():
        monkeypatch.setenv(name, value)


def _settings(**overrides):
    return Settings(**SENTINELS, **overrides)


def _assert_hidden(text: str):
    leaked = [value for value in (*SENTINELS.values(), *ENCRYPTION_KEYS.values()) if value in text]
    assert not leaked, f"secret values shown: {leaked}"


def test_every_secret_setting_is_a_secret_str():
    for name in SECRET_FIELDS:
        assert Settings.model_fields[name].annotation is SecretStr, name


def test_the_secrets_still_reach_their_users():
    settings = _settings()
    for name, value in SENTINELS.items():
        assert getattr(settings, name).get_secret_value() == value
    assert settings.database_url.password == SENTINELS["db_password"]


@pytest.mark.usefixtures("encryption_keys")
@pytest.mark.parametrize("render", [
    repr, str, lambda s: str(s.model_dump()), lambda s: s.model_dump_json(), lambda s: str(dict(s)),
    lambda s: str(s.database_url), lambda s: repr(s.database_url),
    lambda s: f"{s!r} {s!s}", lambda s: str(vars(s)),
], ids=["repr", "str", "dump", "dump_json", "dict", "db_url_str", "db_url_repr", "fstring", "vars"])
def test_no_rendering_shows_a_secret(render):
    _assert_hidden(render(_settings()))


def test_an_attribute_error_shows_no_secret():
    with pytest.raises(AttributeError) as raised:
        _settings().no_such_setting  # noqa: B018
    _assert_hidden(str(raised.value))


@pytest.mark.usefixtures("encryption_keys")
@pytest.mark.parametrize("overrides", [
    {"email_delivery": "logs"},                                   # a field validator fails
    {"app_env": "production", "jwt_issuer": ""},                  # a model validator fails
    {"email_delivery": "ses", "ses_from_email": ""},              # another model validator
    {"db_pool_size": "not-a-number"},                             # a type error
])
def test_a_validation_error_shows_no_secret(overrides):
    with pytest.raises(ValidationError) as raised:
        _settings(**overrides)
    _assert_hidden(str(raised.value))
    _assert_hidden(repr(raised.value.errors()))
