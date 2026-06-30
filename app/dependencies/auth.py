"""
RBAC dependency layer.

Validates RS256 JWTs issued by the external User Management Service.
Roles and permissions are taken directly from the token — no hard-coded mapping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# JWKS cache
# Fetches the auth service's public keys; retries on unknown kid (key rotation).
# ---------------------------------------------------------------------------
class _JwksCache:
    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._ttl: float = 3600.0

    async def get_key(self, kid: str) -> dict:
        if not self._keys or (time.monotonic() - self._fetched_at) > self._ttl:
            await self._refresh()

        if kid not in self._keys:
            await self._refresh()  # one forced re-fetch to handle key rotation

        if kid not in self._keys:
            raise UnauthorizedError(f"Unknown signing key id: {kid}")

        return self._keys[kid]

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(settings.auth_jwks_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise UnauthorizedError(f"Cannot reach JWKS endpoint: {exc}")

        jwks = resp.json()
        self._keys = {k["kid"]: k for k in jwks.get("keys", [])}
        self._fetched_at = time.monotonic()


_jwks_cache = _JwksCache()


# ---------------------------------------------------------------------------
# Resolved user — carries exactly what the token contains, nothing more.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthUser:
    user_id: int
    email: str
    name: str
    roles: frozenset[str]        # e.g. {"Hr-Manager", "Manager"}
    permissions: frozenset[str]  # e.g. {"VIEW_USER_ALL", "APPROVE_TIMESHEET", ...}

    def has_role(self, *roles: str) -> bool:
        """True if the user has ALL of the given roles."""
        return all(r in self.roles for r in roles)

    def has_any_role(self, *roles: str) -> bool:
        """True if the user has AT LEAST ONE of the given roles."""
        return any(r in self.roles for r in roles)

    def has_permission(self, *perms: str) -> bool:
        """True if the user has ALL of the given permissions."""
        return all(p in self.permissions for p in perms)

    def has_any_permission(self, *perms: str) -> bool:
        """True if the user has AT LEAST ONE of the given permissions."""
        return any(p in self.permissions for p in perms)


# ---------------------------------------------------------------------------
# Core dependency: validate JWT → return AuthUser
# ---------------------------------------------------------------------------
async def _get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser:
    if not credentials:
        raise UnauthorizedError("Missing bearer token")

    token = credentials.credentials

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise UnauthorizedError("Malformed JWT header")

    kid = header.get("kid")
    if not kid:
        raise UnauthorizedError("JWT missing 'kid' header")

    jwk = await _jwks_cache.get_key(kid)

    try:
        payload = jwt.decode(
            token,
            jwk,
            algorithms=["RS256"],
            issuer=settings.auth_service_issuer,
        )
    except ExpiredSignatureError:
        raise UnauthorizedError("Token has expired")
    except JWTError as exc:
        raise UnauthorizedError(f"Token validation failed: {exc}")

    user_id = payload.get("user_id")
    if not user_id:
        raise UnauthorizedError("Token missing 'user_id' claim")

    return AuthUser(
        user_id=int(user_id),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=frozenset(payload.get("roles", [])),
        permissions=frozenset(payload.get("permissions", [])),
    )


# ---------------------------------------------------------------------------
# Guard factories — pass role/permission strings exactly as they appear in the token
# ---------------------------------------------------------------------------

def require_roles(*roles: str):
    """
    Dependency factory: user must have ALL of the listed roles.

    Usage in a route:
        @router.post("/")
        async def create(user: Annotated[AuthUser, Security(require_roles("Hr-Manager"))]):
    """
    async def guard(user: Annotated[AuthUser, Depends(_get_current_user)]) -> AuthUser:
        if not user.has_role(*roles):
            raise ForbiddenError(
                f"Requires roles {list(roles)}. User has: {sorted(user.roles)}"
            )
        return user
    return guard


def require_any_role(*roles: str):
    """
    Dependency factory: user must have AT LEAST ONE of the listed roles.

    Usage in a route:
        @router.get("/")
        async def list_items(user: Annotated[AuthUser, Security(require_any_role("Hr-Manager", "Manager"))]):
    """
    async def guard(user: Annotated[AuthUser, Depends(_get_current_user)]) -> AuthUser:
        if not user.has_any_role(*roles):
            raise ForbiddenError(
                f"Requires at least one of {list(roles)}. User has: {sorted(user.roles)}"
            )
        return user
    return guard


def require_permissions(*perms: str):
    """
    Dependency factory: user must have ALL of the listed permissions.

    Usage in a route:
        @router.delete("/{id}")
        async def delete(id: int, user: Annotated[AuthUser, Security(require_permissions("DELETE_PROJECT_BY_ID"))]):
    """
    async def guard(user: Annotated[AuthUser, Depends(_get_current_user)]) -> AuthUser:
        missing = [p for p in perms if p not in user.permissions]
        if missing:
            raise ForbiddenError(f"Missing required permissions: {missing}")
        return user
    return guard


# ---------------------------------------------------------------------------
# Convenience alias — any valid token, role not checked
# ---------------------------------------------------------------------------
CurrentUser = Annotated[AuthUser, Depends(_get_current_user)]
