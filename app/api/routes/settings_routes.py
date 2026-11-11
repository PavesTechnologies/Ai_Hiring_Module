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
from app.services.settings.alert_settings import AlertSettingsService
from app.services.settings.platform_settings import InvalidSetting
from app.services.settings.platform_settings_service import PlatformSettingsService
from app.services.settings.schedules import list_schedules

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


def get_alert_settings_service(db: Session = Depends(get_db)) -> AlertSettingsService:
    return AlertSettingsService(db, AuditService(AuditRepository(db)))


@router.get("/alerts", response_model=APIResponse[list], status_code=status.HTTP_200_OK,
            summary="The internal alerts and who receives each (every active HR admin unless opted out)")
def list_alerts(service: AlertSettingsService = Depends(get_alert_settings_service),
                user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.list())


@router.put("/alerts/{trigger_event}/recipients/{user_id}", response_model=APIResponse[list],
            status_code=status.HTTP_200_OK, summary="Switch one HR admin on or off for one alert (audited)")
def set_alert_recipient(trigger_event: str, user_id: str, payload: dict = Body(...),
                        service: AlertSettingsService = Depends(get_alert_settings_service),
                        user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    return APIResponse.ok(data=service.set_recipient(trigger_event, user_id, enabled, actor_id=user.user_id),
                          message="Alert recipients saved.")


@router.get("/schedules", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="The scheduled jobs and when they run (read-only)")
def get_schedules(user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    from app.core.celery_app import celery_app
    return APIResponse.ok(data=list_schedules(celery_app))
