"""
The HR_ADMIN scoring monitoring view (Phase 4): outcomes over time, vote quality, LLM usage and cost,
quota and task health, and the fairness report - next to the ops endpoints in monitoring_routes.py
(same /monitoring prefix).
"""
import json
import uuid
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.middleware.rbac import TokenUser, require_roles
from app.models.config import PlatformConfig
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.repositories.config_repository import ConfigRepository
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.monitoring.fairness_report import MIN_GROUP_SIZE_KEY, fairness_report, min_group_size
from app.services.monitoring.llm_prices import LLM_MODEL_PRICES_KEY, InvalidPrices, load_prices, validate_prices
from app.services.monitoring.monitoring_service import monitoring_summary

router = APIRouter(prefix="/monitoring", tags=["Scoring Monitoring"])
_PLATFORM_CONFIG_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")


@router.get(
    "/scoring-summary",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Scoring monitoring: outcomes over time, vote quality, LLM usage and cost, quota, tasks",
    description=("Counts and rates only - no candidate details. Costs are estimates at list price "
                 "(LLM_MODEL_PRICES). HR_ADMIN only."),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_scoring_summary(
    days: int = Query(default=30, ge=1, le=365),
    campaign_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return APIResponse.ok(data=monitoring_summary(db, ConfigRepository(db), days=days, campaign_id=campaign_id),
                          message="Scoring monitoring summary.")


@router.get(
    "/llm-prices",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="LLM list prices per model (USD per 1M tokens), for the cost estimates",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_llm_prices(db: Session = Depends(get_db)):
    return APIResponse.ok(data=load_prices(ConfigRepository(db)), message="LLM prices.")


@router.put(
    "/llm-prices",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Set the LLM list prices per model",
    description='Body: {"models": {"<model>": {"input": 0.25, "output": 1.5, "source": "..."}}}. Audited.',
)
def put_llm_prices(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    try:
        prices = validate_prices(payload)
    except InvalidPrices as exc:
        raise CampaignException(str(exc), 422)
    repo = ConfigRepository(db)
    before = load_prices(repo)
    value = json.dumps(prices, indent=1)
    row = db.query(PlatformConfig).filter(PlatformConfig.key == LLM_MODEL_PRICES_KEY).first()
    if row is None:
        db.add(PlatformConfig(id=uuid.uuid4(), key=LLM_MODEL_PRICES_KEY, value=value, updated_by=user.user_id,
                              description="LLM list prices per model (USD per 1M input / output tokens), for the "
                                          "monitoring view's cost estimates."))
    else:
        row.value = value
        row.updated_by = user.user_id
    AuditService(AuditRepository(db)).log(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value,
        action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
        entity_id=_PLATFORM_CONFIG_AUDIT_ENTITY_ID, campaign_id=None,
        details={"title": "LLM list prices updated", "before": before, "after": prices},
    )
    db.commit()
    return APIResponse.ok(data=load_prices(repo), message="LLM prices saved.")


@router.get(
    "/fairness-report",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Fairness: screening pass rates across groups the system can see without inferring protected attributes",
    description=("Experience band, career gap, highest education, region (state / country) and internships, "
                 "for all candidates and for those meeting the JD's minimum experience. Groups below "
                 "FAIRNESS_MIN_GROUP_SIZE are hidden. Includes notes on what it can and can't show. HR_ADMIN only."),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_fairness_report(
    campaign_id: UUID | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=3650, description="Decisions in the last N days; all when empty."),
    db: Session = Depends(get_db),
):
    return APIResponse.ok(data=fairness_report(db, ConfigRepository(db), campaign_id=campaign_id, days=days),
                          message="Fairness report.")


@router.put(
    "/fairness-settings",
    response_model=APIResponse[dict],
    status_code=status.HTTP_200_OK,
    summary="Set the fairness report's minimum group size",
    description='Body: {"min_group_size": 10} (2-1000). Audited.',
)
def put_fairness_settings(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    size = payload.get("min_group_size")
    if isinstance(size, bool) or not isinstance(size, int) or not 2 <= size <= 1000:
        raise CampaignException("min_group_size must be a whole number from 2 to 1000.", 422)
    before = min_group_size(ConfigRepository(db))
    row = db.query(PlatformConfig).filter(PlatformConfig.key == MIN_GROUP_SIZE_KEY).first()
    if row is None:
        db.add(PlatformConfig(id=uuid.uuid4(), key=MIN_GROUP_SIZE_KEY, value=str(size), updated_by=user.user_id,
                              description="Fairness report: groups with fewer candidates than this are not shown."))
    else:
        row.value = str(size)
        row.updated_by = user.user_id
    AuditService(AuditRepository(db)).log(
        actor_id=user.user_id, actor_role=UserRole.HR_ADMIN.value,
        action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
        entity_id=_PLATFORM_CONFIG_AUDIT_ENTITY_ID, campaign_id=None,
        details={"title": "Fairness report minimum group size updated", "before": before, "after": size},
    )
    db.commit()
    return APIResponse.ok(data={"min_group_size": size}, message="Fairness settings saved.")
