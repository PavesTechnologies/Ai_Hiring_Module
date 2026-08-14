import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.dependencies.oauth import get_microsoft_oauth_service
from app.middleware.rbac import TokenUser, get_current_user
from app.schemas.oauth_schema import OAuthConnectResponse, OAuthStatusResponse
from app.schemas.response import APIResponse
from app.services.microsoft_oauth_service import MicrosoftOAuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/microsoft", tags=["Microsoft OAuth"])

_LANDING_PATH = "/airs/settings"


def _landing_url(connect_status: str) -> str:
    base = settings.frontend_base_url.rstrip("/") if settings.frontend_base_url else ""
    return f"{base}{_LANDING_PATH}?connected=microsoft&status={connect_status}"


@router.get(
    "/connect",
    response_model=APIResponse[OAuthConnectResponse],
    summary="Start Microsoft Calendar Connect Flow",
    description=(
        "Authenticated JSON endpoint, not a raw redirect - JWTMiddleware "
        "requires a valid Authorization header on every non-public path, "
        "and a literal backend 302 triggered by a plain top-level browser "
        "navigation would never carry one either. The frontend calls this "
        "via fetch (with its normal auth header) and performs the actual "
        "window.location navigation itself with the returned auth_url."
    ),
)
def connect(
    service: MicrosoftOAuthService = Depends(get_microsoft_oauth_service),
    user: TokenUser = Depends(get_current_user),
):
    auth_url = service.build_authorize_url(user.user_id)
    return APIResponse.ok(data=OAuthConnectResponse(auth_url=auth_url))


@router.get(
    "/callback",
    status_code=status.HTTP_302_FOUND,
    summary="Microsoft OAuth Callback",
    description=(
        "Public path (see JWTMiddleware._PUBLIC_PATHS) - Microsoft "
        "redirects the user's browser here directly with no Authorization "
        "header at all. `state` (see app.core.oauth_state) is what proves "
        "this is a genuine continuation of a connect flow this app "
        "initiated for a specific user, within a short time window - that "
        "takes the place of our own bearer auth for this one endpoint. "
        "Always redirects the browser on to the frontend settings page "
        "(success or failure) rather than returning a raw JSON error, "
        "since the user just came from a full-page Microsoft consent "
        "screen, not a fetch call."
    ),
)
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    service: MicrosoftOAuthService = Depends(get_microsoft_oauth_service),
):
    if error or not code or not state:
        logger.warning("Microsoft OAuth callback failed before token exchange: error=%s", error)
        return RedirectResponse(url=_landing_url("error"), status_code=status.HTTP_302_FOUND)

    try:
        service.handle_callback(code=code, state=state)
    except Exception:
        logger.exception("Microsoft OAuth callback failed during token exchange/storage.")
        return RedirectResponse(url=_landing_url("error"), status_code=status.HTTP_302_FOUND)

    return RedirectResponse(url=_landing_url("success"), status_code=status.HTTP_302_FOUND)


@router.get(
    "/status",
    response_model=APIResponse[OAuthStatusResponse],
    summary="Microsoft Calendar Connection Status",
    description=(
        "Lets the frontend show TEAMS scheduling as fully automatic "
        "(connected) vs. manual-link fallback (not connected) before the "
        "scheduler ever opens the schedule form."
    ),
)
def connection_status(
    service: MicrosoftOAuthService = Depends(get_microsoft_oauth_service),
    user: TokenUser = Depends(get_current_user),
):
    return APIResponse.ok(data=OAuthStatusResponse(connected=service.is_connected(user.user_id)))
