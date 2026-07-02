import logging
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.middleware.jwks import get_issuer, get_jwks_key

logger = logging.getLogger(__name__)


# ── Token decoding ────────────────────────────────────────────────────────────

def decode_token(token: str) -> dict:
    """
    Validate a Bearer JWT and return its decoded claims.

    Raises ValueError with a user-safe message on any failure.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise ValueError("Malformed token header") from exc

    kid = header.get("kid")

    try:
        key = get_jwks_key(kid)
    except RuntimeError as exc:
        raise ValueError("Authentication service unavailable") from exc

    issuer = get_issuer()
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer or None,
            options={"verify_aud": False, "verify_iss": bool(issuer)})
    except JWTError as exc:
        low = str(exc).lower()
        if "expired" in low:
            raise ValueError("Token has expired") from exc
        if "issuer" in low:
            raise ValueError("Token issuer is invalid") from exc
        raise ValueError("Token signature or claims are invalid") from exc

    return payload


# ── Middleware ────────────────────────────────────────────────────────────────

_PUBLIC_PATHS = ["/docs", "/openapi.json", "/redoc", "/health"]


class JWTMiddleware(BaseHTTPMiddleware):
    """
    Validates the Bearer JWT on every inbound request that carries one.
    Attaches the decoded claims dict to request.state.token_payload.
    Requests without an Authorization: Bearer … header pass through unchanged.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        logger.debug("JWTMiddleware dispatch called")
        request.state.token_payload = None
        path = request.url.path

        if request.method == "OPTIONS" or any(path.startswith(p) for p in _PUBLIC_PATHS):
            logger.debug("Skipping JWT validation for path: %s", path)
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"status_code": 401, "message": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth.removeprefix("Bearer ")
        try:
            print("token received in middleware", token[:10])
            request.state.token_payload = decode_token(token)
        except ValueError as exc:
            logger.warning("JWT rejected | path=%s | reason=%s", request.url.path, exc)
            return JSONResponse(
                status_code=401,
                content={"status_code": 401, "message": str(exc)},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
