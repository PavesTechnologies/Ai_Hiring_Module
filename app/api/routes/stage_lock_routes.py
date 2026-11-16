"""
Stage lock (email plan, phase 4): an HR admin unlocks a stage locked by a rejection or selection email, with a
reason code (audited). app/services/campaign/stage_lock.py.
"""
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Security
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.campaign import stage_lock
from app.services.campaign_access import CampaignAccess

router = APIRouter(tags=["Stage lock"])


@router.get("/stage-lock/unlock-reasons", response_model=APIResponse[list[dict]],
            summary="The reasons an HR admin can give for unlocking a locked stage")
def list_stage_unlock_reasons(db: Session = Depends(get_db),
                              user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    return APIResponse.ok(data=[{"code": c, "label": l} for c, l in stage_lock.unlock_reason_codes(db).items()])


@router.post("/campaign-candidates/{campaign_candidate_id}/stage-unlock", response_model=APIResponse[dict],
             summary="Unlock a stage locked by a rejection or selection email (HR admin, reason code, audited)")
def unlock_stage(campaign_candidate_id: UUID, payload: dict = Body(...), db: Session = Depends(get_db),
                 user: TokenUser = Security(require_roles(UserRole.HR_ADMIN))):
    CampaignAccess(db).require_campaign_candidate(user, campaign_candidate_id)
    reason_code = (payload or {}).get("reason_code") if isinstance(payload, dict) else None
    result = stage_lock.unlock(db, campaign_candidate_id, reason_code=reason_code, actor=user,
                               audit_service=AuditService(AuditRepository(db)))
    return APIResponse.ok(data=result, message="Stage unlocked.")
