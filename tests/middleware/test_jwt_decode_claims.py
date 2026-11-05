"""decode_token: exp is required, and iss must be exactly JWT_ISSUER (UMS sets no aud). A throwaway RSA key."""
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app.core.config import settings
from app.middleware import jwt_middleware

_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode()
_PUBLIC_JWK = jwk.construct(_private.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(), "RS256").to_dict()
ISSUER = "https://ums.example.invalid/ums"
DISCOVERY_ISSUER = "https://discovery.example.invalid/ums"


def _token(**claims):
    base = {"user_id": "u1", "email": "a@example.invalid", "roles": ["HR_ADMIN"], "iss": ISSUER,
            "exp": int(time.time()) + 600}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, _PRIVATE_PEM, algorithm="RS256",
                      headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "jwt_issuer", ISSUER)
    with patch.object(jwt_middleware, "get_jwks_key", return_value=_PUBLIC_JWK), \
            patch.object(jwt_middleware, "get_issuer", return_value=DISCOVERY_ISSUER):
        yield


def test_a_valid_token_decodes():
    assert jwt_middleware.decode_token(_token())["user_id"] == "u1"


def test_a_token_without_exp_is_refused():
    with pytest.raises(ValueError, match="Token has no expiry"):
        jwt_middleware.decode_token(_token(exp=None))


def test_an_expired_token_is_refused():
    with pytest.raises(ValueError, match="Token has expired"):
        jwt_middleware.decode_token(_token(exp=int(time.time()) - 60))


def test_aud_is_not_checked():
    assert jwt_middleware.decode_token(_token(aud="another-app"))["user_id"] == "u1"


@pytest.mark.parametrize("iss", ["https://evil.example.invalid/ums", "https://ums.example.invalid/ums/",
                                 "https://ums.example.invalid", DISCOVERY_ISSUER])
def test_any_other_issuer_is_refused(iss):
    with pytest.raises(ValueError, match="Token issuer is invalid"):
        jwt_middleware.decode_token(_token(iss=iss))


def test_a_token_without_iss_is_refused():
    with pytest.raises(ValueError, match="Token issuer is invalid"):
        jwt_middleware.decode_token(_token(iss=None))


def test_without_jwt_issuer_the_discovery_issuer_is_required(monkeypatch):
    monkeypatch.setattr(settings, "jwt_issuer", "")
    assert jwt_middleware.decode_token(_token(iss=DISCOVERY_ISSUER))["user_id"] == "u1"
    with pytest.raises(ValueError, match="Token issuer is invalid"):
        jwt_middleware.decode_token(_token(iss=ISSUER))


@pytest.mark.parametrize("app_env", ["production", " Prod "])
def test_production_refuses_to_start_without_jwt_issuer(app_env):
    from pydantic import ValidationError
    from app.core.config import Settings
    with pytest.raises(ValidationError, match="needs JWT_ISSUER"):
        Settings(app_env=app_env, jwt_issuer=" ")
    assert Settings(app_env=app_env, jwt_issuer=ISSUER).jwt_issuer == ISSUER


def test_development_starts_without_jwt_issuer():
    from app.core.config import Settings
    assert Settings(app_env="development", jwt_issuer="").jwt_issuer == ""
