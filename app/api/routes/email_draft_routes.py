"""
Email drafts (email plan, phase 3): a candidate's or interviewer's email is sent only when a person previews it
and clicks Send, confirming a step that names the recipient. app/services/notifications/drafts.py.
"""
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.exceptions.campaign_exceptions import CampaignException
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.campaign_access import CampaignAccess
from app.services.notifications import drafts

router = APIRouter(tags=["Email drafts"])
_ROLES = (UserRole.HR_ADMIN, UserRole.HIRING_MANAGER, UserRole.RECRUITER)


def _view(row) -> dict:
    return {
        "id": str(row.id), "status": row.status.value, "trigger_event": row.trigger_event.value,
        "recipient_type": row.recipient_type.value, "recipient_name": row.recipient_name,
        "interview_schedule_id": str(row.interview_schedule_id) if row.interview_schedule_id else None,
        "interview_interviewer_id": str(row.interview_interviewer_id) if row.interview_interviewer_id else None,
        "prepared_by": row.prepared_by, "prepared_at": row.prepared_at.isoformat() if row.prepared_at else None,
        "confirmed_by": row.confirmed_by, "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None, "block_code": row.block_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _call(fn):
    try:
        return fn()
    except drafts.DraftError as exc:
        raise CampaignException(exc.message, exc.status_code, data=exc.data)


@router.get("/campaign-candidates/{campaign_candidate_id}/email-drafts", response_model=APIResponse[list[dict]],
            summary="A candidate's emails waiting for Send (include_all: the rest of its email history too)")
def list_email_drafts(campaign_candidate_id: UUID, include_all: bool = False, db: Session = Depends(get_db),
                      user: TokenUser = Security(require_roles(*_ROLES))):
    CampaignAccess(db).require_campaign_candidate(user, campaign_candidate_id)
    rows = drafts.list_for_candidate(db, campaign_candidate_id, include_all=include_all)
    return APIResponse.ok(data=[_view(r) for r in rows])


@router.post("/email-drafts/{draft_id}/preview", response_model=APIResponse[dict],
             summary="The email exactly as it will be sent, its recipient, and why it can't be sent (if it can't)")
def preview_email_draft(draft_id: UUID, db: Session = Depends(get_db),
                        user: TokenUser = Security(require_roles(*_ROLES))):
    row, preview = _call(lambda: drafts.get_preview(db, draft_id, actor=user))
    return APIResponse.ok(data={
        **_view(row), "recipient_name": preview.recipient_name, "recipient_address": preview.recipient_address,
        "subject": preview.subject, "body": preview.body, "preview_hash": preview.preview_hash,
        "problems": [{"code": p.code, "message": p.message} for p in preview.problems],
        "can_send": row.status.value == "DRAFT" and not preview.problems,
    })


@router.post("/email-drafts/{draft_id}/send", response_model=APIResponse[dict], status_code=status.HTTP_200_OK,
             summary="Send a previewed email (the preview_hash from the preview; refused if it changed since)")
def send_email_draft(draft_id: UUID, payload: dict = Body(...), db: Session = Depends(get_db),
                     user: TokenUser = Security(require_roles(*_ROLES))):
    preview_hash = payload.get("preview_hash") if isinstance(payload, dict) else None
    audit = AuditService(AuditRepository(db))
    row = _call(lambda: drafts.send(db, draft_id, actor=user, preview_hash=preview_hash, audit_service=audit))
    return APIResponse.ok(data=_view(row), message="Email queued for sending.")


@router.post("/email-drafts/{draft_id}/discard", response_model=APIResponse[dict],
             summary="Discard an email waiting for Send")
def discard_email_draft(draft_id: UUID, db: Session = Depends(get_db),
                        user: TokenUser = Security(require_roles(*_ROLES))):
    audit = AuditService(AuditRepository(db))
    row = _call(lambda: drafts.discard(db, draft_id, actor=user, audit_service=audit))
    return APIResponse.ok(data=_view(row), message="Email discarded.")
