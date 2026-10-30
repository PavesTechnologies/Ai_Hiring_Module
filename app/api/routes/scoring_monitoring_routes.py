"""
The HR_ADMIN scoring monitoring view (Phase 4): outcomes over time, vote quality, LLM usage and cost,
quota and task health - next to the ops endpoints in monitoring_routes.py (same /monitoring prefix).
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
