import logging
from dataclasses import dataclass, field
from typing import Callable
from uuid import UUID

from fastapi import Request

from app.enums.constants import UserRole
from app.exception_handler.exceptions import ForbiddenError, UnauthorizedError

logger = logging.getLogger(__name__)


# ── Token helpers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUser:
    user_id: UUID  # this platform's users.id, from the token's obs_user_uuid claim
    email: str
    roles: list[str]
    claims: dict = field(default_factory=dict)


# ── Shared payload → TokenUser conversion ─────────────────────────────────────

def _get_payload(request: Request) -> dict:
    payload: dict | None = getattr(request.state, "token_payload", None)

    if payload is None:
        raise UnauthorizedError("Authentication required")

    return payload


def _to_token_user(payload: dict) -> TokenUser:
    token_roles = payload.get("roles", [])
    user_id = payload.get("user_id")

    if isinstance(token_roles, str):
        token_roles = [token_roles]

    # "user_id" is the UMS's own numeric account id — not a UUID and not a
    # foreign key into our local `users` table. "obs_user_uuid" is the id
    # that actually maps to users.id in this platform's database.
    # raw_id = payload.get("obs_user_uuid")
    # try:
    #     user_id = UUID(str(raw_id))
    # except (TypeError, ValueError):
    #     raise UnauthorizedError("Token missing a valid platform user id (obs_user_uuid)")

    return TokenUser(
        user_id=user_id,
        email=payload.get("email", ""),
        roles=token_roles,
        claims=payload,
    )


# ── Dependency factory ────────────────────────────────────────────────────────

def require_roles(*allowed_roles: UserRole) -> Callable:
    allowed = frozenset(role.value for role in allowed_roles)

    def _check(request: Request) -> TokenUser:
        user = _to_token_user(_get_payload(request))

        if allowed and not any(role in allowed for role in user.roles):
            raise ForbiddenError(
                f"Access denied. Required: {' or '.join(sorted(allowed))}"
            )

        return user

    return _check


# ── Current-user accessors ─────────────────────────────────────────────────────

def get_current_user(request: Request) -> TokenUser:
    """
    Dependency: any valid, authenticated token — no role check.

    Usage in a route:
        @router.get("/")
        async def whoami(user: TokenUser = Depends(get_current_user)):
    """
    return _to_token_user(_get_payload(request))


def get_current_user_id(request: Request):
    """
    Dependency: just the caller's platform user id, unwrapped from the JWT claims.

    Usage in a route:
        @router.post("/")
        async def create(user_id: UUID = Depends(get_current_user_id)):
    """
    return _to_token_user(_get_payload(request)).user_id