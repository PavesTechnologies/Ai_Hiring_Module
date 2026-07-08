import logging
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.enums.constants import UserRole
from app.exception_handler.exceptions import ForbiddenError, UnauthorizedError
from app.models.identity import User
from app.models.identity import UserRole as LocalUserRole

logger = logging.getLogger(__name__)


# ── Token helpers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUser:
    user_id: str  # this platform's users.id, unwrapped from the token's user_id claim
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

    if isinstance(token_roles, str):
        token_roles = [token_roles]

    raw_id = payload.get("user_id")
    if raw_id is None:
        raise UnauthorizedError("Token missing 'user_id' claim")

    return TokenUser(
        user_id=str(raw_id),
        email=payload.get("email", ""),
        roles=token_roles,
        claims=payload,
    )


# ── Local user provisioning ────────────────────────────────────────────────────

# Every FK column in the schema that references users.id, discovered live so
# the re-key logic below never goes stale when new tables are added.
_USER_FK_REFS_SQL = text("""
    SELECT tc.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
    JOIN information_schema.constraint_column_usage ccu
        ON ccu.constraint_name = tc.constraint_name
    WHERE tc.constraint_type = 'FOREIGN KEY'
      AND ccu.table_name = 'users' AND ccu.column_name = 'id'
""")


def _new_user_from_token(user: TokenUser) -> User:
    role_name = next((r for r in user.roles if r in LocalUserRole.__members__), None)

    return User(
        id=user.user_id,
        email=user.email,
        password_hash="EXTERNAL_AUTH",  # identity lives in the UMS, not here
        role=LocalUserRole[role_name] if role_name else LocalUserRole.RECRUITER,
        full_name=user.claims.get("name") or user.email,
        is_active=True,
    )


def _rekey_local_user(db: Session, old_id: str, user: TokenUser) -> None:
    """
    Same email arrived with a new UMS id (the auth service was switched or
    re-issued its ids). Move the shadow row and every row referencing it to
    the new id so attribution history survives instead of colliding with the
    users.email UNIQUE constraint.
    """
    logger.info("Re-keying local user %s -> %s (%s)", old_id, user.user_id, user.email)

    # Free the unique email, insert the new identity, repoint every FK, drop the old row.
    db.execute(
        text("UPDATE users SET email = email || '.superseded.' || id WHERE id = :old"),
        {"old": old_id},
    )
    db.add(_new_user_from_token(user))
    db.flush()
    for table, column in db.execute(_USER_FK_REFS_SQL).all():
        db.execute(
            text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old'),
            {"new": user.user_id, "old": old_id},
        )
    db.execute(text("DELETE FROM users WHERE id = :old"), {"old": old_id})


def _ensure_local_user(user: TokenUser, db: Session) -> None:
    """
    Ensure a local `users` row exists for this token's platform user id.

    The UMS is the source of truth for identity; this app only needs a
    shadow row so created_by/updated_by/actor_id foreign keys resolve.
    Provisions one from the token's own claims on first sight instead of
    requiring a manual sync step.
    """
    if db.get(User, user.user_id) is not None:
        return

    existing = db.query(User).filter(User.email == user.email).first()
    if existing is not None:
        _rekey_local_user(db, existing.id, user)
    else:
        db.add(_new_user_from_token(user))

    db.commit()


# ── Dependency factory ────────────────────────────────────────────────────────

def require_roles(*allowed_roles: UserRole) -> Callable:
    allowed = frozenset(role.value for role in allowed_roles)

    def _check(request: Request, db: Session = Depends(get_db)) -> TokenUser:
        user = _to_token_user(_get_payload(request))

        if allowed and not any(role in allowed for role in user.roles):
            raise ForbiddenError(
                f"Access denied. Required: {' or '.join(sorted(allowed))}"
            )

        _ensure_local_user(user, db)

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


def get_current_user_id(request: Request) -> str:
    """
    Dependency: just the caller's platform user id, unwrapped from the JWT claims.

    Usage in a route:
        @router.post("/")
        async def create(user_id: str = Depends(get_current_user_id)):
    """
    return _to_token_user(_get_payload(request)).user_id