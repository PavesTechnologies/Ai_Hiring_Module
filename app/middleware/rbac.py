import logging
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Request

from app.models.identity import UserRole
from app.exception_handler.exceptions import ForbiddenError, UnauthorizedError

logger = logging.getLogger(__name__)


# ── Token helpers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUser:
    user_id: str
    email: str
    roles: list[str]
    claims: dict = field(default_factory=dict)


# ── Dependency factory ────────────────────────────────────────────────────────

def require_roles(*allowed_roles: UserRole) -> Callable:
    allowed = frozenset(role.value for role in allowed_roles)

    def _check(request: Request) -> TokenUser:
        payload: dict | None = getattr(request.state, "token_payload", None)

        if payload is None:
            raise UnauthorizedError("Authentication required")

        token_roles = payload.get("roles", [])

        if isinstance(token_roles, str):
            token_roles = [token_roles]

        if allowed and not any(role in allowed for role in token_roles):
            raise ForbiddenError(
                f"Access denied. Required: {' or '.join(sorted(allowed))}"
            )

        return TokenUser(
            user_id=payload.get("user_id", ""),
            email=payload.get("email", ""),
            roles=token_roles,
            claims=payload,
        )

    return _check