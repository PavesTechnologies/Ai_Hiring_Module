import logging
import threading
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_issuer: str = ""
_jwks_uri: str = ""
_keys: dict[str, dict[str, Any]] = {}
_fetched_at: float = 0.0
_lock = threading.Lock()
_TTL = 3600  # seconds before re-fetching keys


def _fetch_discovery() -> None:
    url = f"{settings.ums_url}/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OIDC discovery failed: {exc}") from exc
    doc = resp.json()
    global _issuer, _jwks_uri
    _issuer = doc.get("issuer", "")
    _jwks_uri = doc.get("jwks_uri", f"{settings.ums_url}/.well-known/jwks.json")
    logger.info("OIDC discovery complete | issuer=%s | jwks_uri=%s", _issuer, _jwks_uri)


def _fetch_keys() -> None:
    if not _jwks_uri:
        _fetch_discovery()
    try:
        resp = httpx.get(_jwks_uri, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"JWKS fetch failed: {exc}") from exc
    global _keys, _fetched_at
    _keys = {
        k["kid"]: k
        for k in resp.json().get("keys", [])
        if k.get("use") == "sig" and k.get("alg") == "RS256"
    }
    _fetched_at = time.monotonic()
    logger.info("JWKS cached — %d RS256 key(s)", len(_keys))


def get_issuer() -> str:
    return _issuer


def get_jwks_key(kid: str | None) -> dict[str, Any]:
    with _lock:
        stale = (time.monotonic() - _fetched_at) > _TTL
        missing_kid = bool(kid and kid not in _keys)
        if stale or missing_kid or not _issuer:
            if not _issuer or not _jwks_uri:
                _fetch_discovery()
            _fetch_keys()
    if kid and kid in _keys:
        return _keys[kid]
    if len(_keys) == 1:
        # single-key deployments omit kid from the token header
        return next(iter(_keys.values()))
    raise RuntimeError(f"No RS256 key found for kid={kid!r}")


def preload_jwks() -> None:
    """Eagerly fetch OIDC discovery and signing keys at startup."""
    with _lock:
        _fetch_discovery()
        _fetch_keys()
