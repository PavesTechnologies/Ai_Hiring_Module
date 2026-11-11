"""
Settings page API (HR_ADMIN only): platform settings, alert recipients, schedules.

Platform settings: only registered keys (app/services/settings/platform_settings.py), each validated
against its type and range before anything is saved; preview returns the before/after without saving.
"""
from fastapi import APIRouter, Body, Depends, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.exceptions.campaign_exceptions import CampaignException
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.repositories.config_repository import ConfigRepository
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.settings.platform_settings import InvalidSetting
from app.services.settings.platform_settings_service import PlatformSettingsService

router = APIRouter(prefix="/settings", tags=["Settings"])


def get_platform_settings_service(db: Session = Depends(get_db)) -> PlatformSettingsService:
    return PlatformSettingsService(ConfigRepository(db), AuditService(AuditRepository(db)))


def _values(payload: dict) -> dict:
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise CampaignException('Send {"values": {"<KEY>": <number>}}.', 422)
    return values


def _invalid(exc: InvalidSetting) -> CampaignException:
    return CampaignException("Some values can't be saved.", 422, data={"problems": exc.problems})


@router.get("/platform", response_model=APIResponse[list], status_code=status.HTTP_200_OK,
            summary="List the platform settings HR admins can change")
def list_platform_settings(service: PlatformSettingsService = Depends(get_platform_settings_service),
                           user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.list())


@router.post("/platform/preview", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
             summary="Check platform setting changes and show before/after, without saving")
def preview_platform_settings(payload: dict = Body(...),
                              service: PlatformSettingsService = Depends(get_platform_settings_service),
                              user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    try:
        return APIResponse.ok(data=service.preview(_values(payload)))
    except InvalidSetting as exc:
        raise _invalid(exc)


@router.put("/platform", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="Save platform setting changes (audited)")
def save_platform_settings(payload: dict = Body(...),
                           service: PlatformSettingsService = Depends(get_platform_settings_service),
                           user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    try:
        result = service.save(_values(payload), actor_id=user.user_id)
    except InvalidSetting as exc:
        raise _invalid(exc)
    message = "Settings saved." if result["changed"] else "Nothing changed."
    return APIResponse.ok(data=result, message=message)
