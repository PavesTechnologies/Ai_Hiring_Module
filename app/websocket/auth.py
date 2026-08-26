import logging

from fastapi import WebSocket

from app.middleware.jwt_middleware import decode_token
from app.middleware.rbac import TokenUser, _to_token_user

logger = logging.getLogger(__name__)


class WebSocketAuthenticationError(Exception):
    """Raised when WebSocket authentication or authorization fails."""


# Roles allowed to access JD uploads and campaign board
ALL_REALTIME_ROLES = {
    "HR_ADMIN",
    "RECRUITER",
    "HIRING_MANAGER",
}

# Resume processing WebSocket is intentionally recruiter-only
RECRUITER_ONLY = {
    "RECRUITER",
}


async def authenticate_websocket(
    websocket: WebSocket,
) -> TokenUser:
    """
    Authenticate a WebSocket connection using the existing AIRS JWT
    validation and TokenUser implementation.

    The JWT is currently expected as a query parameter:

        ws://host/airs/ws/...?...&token=<JWT>

    This reuses:
        - decode_token() from jwt_middleware.py
        - _to_token_user() from rbac.py
    """

    token = websocket.query_params.get("token")

    if not token:
        logger.warning(
            "WebSocket connection rejected: missing token"
        )
        raise WebSocketAuthenticationError(
            "Authentication token is required"
        )

    try:
        # Reuse existing JWT validation.
        payload = decode_token(token)

        # Reuse existing token → TokenUser conversion.
        user = _to_token_user(payload)

        logger.info(
            "WebSocket authentication successful: "
            "user_id=%s roles=%s",
            user.user_id,
            user.roles,
        )

        return user

    except ValueError as exc:
        # decode_token() already converts JWT errors into
        # user-safe ValueError messages.
        logger.warning(
            "WebSocket JWT rejected: %s",
            exc,
        )

        raise WebSocketAuthenticationError(
            str(exc)
        ) from exc

    except Exception as exc:
        logger.exception(
            "Unexpected WebSocket authentication failure"
        )

        raise WebSocketAuthenticationError(
            "Authentication failed"
        ) from exc


def has_required_role(
    user: TokenUser,
    allowed_roles: set[str],
) -> bool:
    """
    Check whether the authenticated user has at least
    one of the required roles.
    """

    return bool(
        set(user.roles).intersection(allowed_roles)
    )


def require_websocket_role(
    user: TokenUser,
    allowed_roles: set[str],
) -> None:
    """
    Validate WebSocket role access.

    Raises WebSocketAuthenticationError when the user
    does not have the required role.
    """

    if not has_required_role(user, allowed_roles):
        logger.warning(
            "WebSocket authorization denied: "
            "user_id=%s roles=%s required_roles=%s",
            user.user_id,
            user.roles,
            sorted(allowed_roles),
        )

        raise WebSocketAuthenticationError(
            "Access denied"
        )