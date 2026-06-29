import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.config import settings
from app.core.constants import UserRole
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.schemas.response import APIResponse

logger = logging.getLogger(__name__)

# ── JWKS key cache ────────────────────────────────────────────────────────────

_keys: dict[str, dict[str, Any]] = {}
_fetched_at: float = 0.0
_lock = threading.Lock()
_TTL = 3600


def _fetch_keys() -> None:
    uri = f"{settings.ums_url}/.well-known/jwks.json"
    try:
        resp = httpx.get(uri, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch JWKS from UMS: {exc}") from exc
    global _keys, _fetched_at
    _keys = {
        k["kid"]: k
        for k in resp.json().get("keys", [])
        if k.get("use") == "sig" and k.get("alg") == "RS256"
    }
    _fetched_at = time.monotonic()
    logger.info("JWKS loaded — %d key(s) cached", len(_keys))


def get_jwks_key(kid: str | None) -> dict[str, Any]:
    with _lock:
        if (time.monotonic() - _fetched_at) > _TTL or (kid and kid not in _keys):
            _fetch_keys()
    if kid and kid in _keys:
        return _keys[kid]
    if len(_keys) == 1:
        return next(iter(_keys.values()))
    raise RuntimeError(f"No RS256 key found for kid={kid!r}")


def preload_jwks() -> None:
    with _lock:
        _fetch_keys()


# ── Token helpers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUser:
    user_id: str
    email: str
    role: str
    claims: dict = field(default_factory=dict)


def _decode_token(token: str) -> dict:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except JWTError as exc:
        raise ValueError("Malformed token") from exc
    try:
        key = get_jwks_key(kid)
    except RuntimeError as exc:
        raise ValueError("Authentication service unavailable") from exc
    try:
        return jwt.decode(token, key, algorithms=["RS256"],
                          options={"verify_aud": False}, leeway=10)
    except JWTError as exc:
        msg = "Token has expired" if "expired" in str(exc).lower() else "Invalid token"
        raise ValueError(msg) from exc


def _extract_role(payload: dict) -> str:
    role = payload.get("role")
    if isinstance(role, str) and role:
        return role
    roles = payload.get("roles")
    if isinstance(roles, list) and roles:
        return str(roles[0])
    raise ValueError("Token payload missing role claim")


# ── Middleware ────────────────────────────────────────────────────────────────

class RBACMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request.state.user = None
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return await call_next(request)
        token = auth[len("Bearer "):]
        try:
            payload = _decode_token(token)
            role = _extract_role(payload)
        except ValueError as exc:
            logger.warning("Token rejected | path=%s | %s", request.url.path, exc)
            return JSONResponse(
                status_code=401,
                content=APIResponse.fail(str(exc)).model_dump(),
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user = TokenUser(
            user_id=str(payload.get("sub", "")),
            email=str(payload.get("email", "")),
            role=role,
            claims=payload,
        )
        return await call_next(request)


# ── Dependency factory ────────────────────────────────────────────────────────

def require_roles(*allowed_roles: UserRole) -> Callable:
    allowed = frozenset(r.value for r in allowed_roles)

    def _check(request: Request) -> TokenUser:
        user: TokenUser | None = getattr(request.state, "user", None)
        if user is None:
            raise UnauthorizedError("Authentication required")
        if allowed and user.role not in allowed:
            raise ForbiddenError(
                f"Access denied. Required: {' or '.join(sorted(allowed))}"
            )
        return user

    return _check
