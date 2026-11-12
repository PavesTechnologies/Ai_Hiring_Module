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
from app.services.settings.email_template_settings import EmailTemplateSettingsService
from app.services.settings.platform_settings import InvalidSetting
from app.services.settings.platform_settings_service import PlatformSettingsService
from app.services.settings.reason_code_settings import ReasonCodeSettingsService
from app.services.settings.schedules import list_schedules
from app.services.settings.transition_settings import TransitionSettingsService

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


def get_reason_code_settings_service(db: Session = Depends(get_db)) -> ReasonCodeSettingsService:
    return ReasonCodeSettingsService(db, AuditService(AuditRepository(db)))


@router.get("/reason-codes", response_model=APIResponse[list], status_code=status.HTTP_200_OK,
            summary="Every override reason code, retired ones included, with how often each was used")
def list_reason_codes(service: ReasonCodeSettingsService = Depends(get_reason_code_settings_service),
                      user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.list())


@router.post("/reason-codes", response_model=APIResponse[list], status_code=status.HTTP_201_CREATED,
             summary="Add an override reason code (audited)")
def add_reason_code(payload: dict = Body(...),
                    service: ReasonCodeSettingsService = Depends(get_reason_code_settings_service),
                    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.create(payload, actor_id=user.user_id), message="Reason code added.")


@router.patch("/reason-codes/{code}", response_model=APIResponse[list], status_code=status.HTTP_200_OK,
              summary="Change a reason code's label, order, or retire/restore it (audited; never renamed)")
def change_reason_code(code: str, payload: dict = Body(...),
                       service: ReasonCodeSettingsService = Depends(get_reason_code_settings_service),
                       user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.update(code, payload, actor_id=user.user_id), message="Reason code saved.")


def get_email_template_settings_service(db: Session = Depends(get_db)) -> EmailTemplateSettingsService:
    return EmailTemplateSettingsService(db, AuditService(AuditRepository(db)))


@router.get("/email-templates", response_model=APIResponse[list], status_code=status.HTTP_200_OK,
            summary="Every email the system sends, with its template in use")
def list_email_templates(service: EmailTemplateSettingsService = Depends(get_email_template_settings_service),
                         user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.list())


@router.get("/email-templates/{trigger_event}", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="One email's template in use, its versions and the placeholders its sender supplies")
def get_email_template(trigger_event: str,
                       service: EmailTemplateSettingsService = Depends(get_email_template_settings_service),
                       user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.get(trigger_event))


@router.post("/email-templates/{trigger_event}/preview", response_model=APIResponse[dict],
             status_code=status.HTTP_200_OK, summary="Render a template with made-up sample values (sends nothing)")
def preview_email_template(trigger_event: str, payload: dict = Body(...),
                           service: EmailTemplateSettingsService = Depends(get_email_template_settings_service),
                           user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.preview(trigger_event, payload))


@router.put("/email-templates/{trigger_event}", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="Save a new version of an email template (audited)")
def save_email_template(trigger_event: str, payload: dict = Body(...),
                        service: EmailTemplateSettingsService = Depends(get_email_template_settings_service),
                        user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    result = service.save(trigger_event, payload, actor_id=user.user_id)
    return APIResponse.ok(data=result, message="Template saved as a new version." if result["changed"]
                          else "Nothing changed.")


@router.post("/email-templates/{trigger_event}/versions/{template_id}/restore", response_model=APIResponse[dict],
             status_code=status.HTTP_200_OK, summary="Use an older version again, saved as a new version (audited)")
def restore_email_template(trigger_event: str, template_id: str,
                           service: EmailTemplateSettingsService = Depends(get_email_template_settings_service),
                           user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.restore(trigger_event, template_id, actor_id=user.user_id),
                          message="Version restored.")


def get_transition_settings_service(db: Session = Depends(get_db)) -> TransitionSettingsService:
    return TransitionSettingsService(db, AuditService(AuditRepository(db)))


def _changes(payload) -> list:
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if not isinstance(changes, list):
        raise CampaignException('Send {"changes": [...]}.', 422)
    return changes


@router.get("/transitions", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="The stage moves, who may make each, and which are protected")
def list_transitions(service: TransitionSettingsService = Depends(get_transition_settings_service),
                     user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.list())


@router.post("/transitions/preview", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
             summary="Check a batch of stage-move changes against the pipeline rules, without saving")
def preview_transitions(payload: dict = Body(...),
                        service: TransitionSettingsService = Depends(get_transition_settings_service),
                        user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=service.preview(_changes(payload)))


@router.put("/transitions", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
            summary="Save a batch of stage-move changes (all or nothing; audited)")
def save_transitions(payload: dict = Body(...),
                     service: TransitionSettingsService = Depends(get_transition_settings_service),
                     user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    result = service.save(_changes(payload), actor_id=user.user_id)
    return APIResponse.ok(data=result, message="Stage moves saved." if result["changed"] else "Nothing changed.")
