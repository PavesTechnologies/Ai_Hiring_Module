"""decode_token: exp is required, and aud is checked when JWT_AUDIENCE is set (a throwaway RSA key)."""
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
ISSUER = "https://ums.example.invalid"


def _token(**claims):
    base = {"user_id": "u1", "email": "a@example.invalid", "roles": ["HR_ADMIN"], "iss": ISSUER,
            "exp": int(time.time()) + 600}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, _PRIVATE_PEM, algorithm="RS256",
                      headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "jwt_audience", "")
    with patch.object(jwt_middleware, "get_jwks_key", return_value=_PUBLIC_JWK), \
            patch.object(jwt_middleware, "get_issuer", return_value=ISSUER):
        yield


def test_a_valid_token_decodes():
    assert jwt_middleware.decode_token(_token())["user_id"] == "u1"


def test_a_token_without_exp_is_refused():
    with pytest.raises(ValueError, match="Token has no expiry"):
        jwt_middleware.decode_token(_token(exp=None))


def test_an_expired_token_is_refused():
    with pytest.raises(ValueError, match="Token has expired"):
        jwt_middleware.decode_token(_token(exp=int(time.time()) - 60))


def test_audience_is_not_checked_when_unset():
    assert jwt_middleware.decode_token(_token(aud="another-app"))["user_id"] == "u1"


def test_the_configured_audience_is_required(monkeypatch):
    monkeypatch.setattr(settings, "jwt_audience", "airs")
    assert jwt_middleware.decode_token(_token(aud="airs"))["user_id"] == "u1"
    with pytest.raises(ValueError, match="Token audience is invalid"):
        jwt_middleware.decode_token(_token(aud="another-app"))
    with pytest.raises(ValueError, match="Token audience is invalid"):
        jwt_middleware.decode_token(_token())


def test_a_wrong_issuer_is_refused():
    with pytest.raises(ValueError, match="Token issuer is invalid"):
        jwt_middleware.decode_token(_token(iss="https://evil.example.invalid"))
