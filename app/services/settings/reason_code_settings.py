"""
Override reason codes on the Settings page (settings phase 1). HR admins add codes, edit labels and order,
and retire or restore codes. A code is never renamed or deleted (past overrides keep it) and OTHER can't be
retired, so a person can always give a reason. The audit records the code and the flags/numbers that
changed; a label is typed text, recorded only as changed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.identity import UserRole
from app.models.outcome_overrides import CandidateOutcomeOverride, OverrideReasonCode
from app.services.settings.audit_values import audited_changes

_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")
LOCKED_CODE = "OTHER"
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,39}$")
LABEL_MAX = 80


def _label(value) -> str:
    label = (value or "").strip() if isinstance(value, str) else ""
    if not 2 <= len(label) <= LABEL_MAX:
        raise CampaignException(f"Give a label of 2 to {LABEL_MAX} characters.", 422, data={"field": "label"})
    return label


def _sort_order(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise CampaignException("The order must be a whole number from 0 to 10000.", 422,
                                data={"field": "sort_order"})
    return value


class ReasonCodeSettingsService:
    def __init__(self, db, audit_service):
        self.db = db
        self.audit_service = audit_service

    def list(self) -> list[dict]:
        used = dict(self.db.query(CandidateOutcomeOverride.reason_code, func.count())
                    .group_by(CandidateOutcomeOverride.reason_code).all())
        rows = (self.db.query(OverrideReasonCode)
                .order_by(OverrideReasonCode.sort_order, OverrideReasonCode.code).all())
        return [{"code": r.code, "label": r.label, "is_active": r.is_active, "sort_order": r.sort_order,
                 "locked": r.code == LOCKED_CODE, "times_used": int(used.get(r.code, 0))} for r in rows]

    def _audit(self, actor_id: str, details: dict) -> None:
        self.audit_service.log(
            actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
            action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
            entity_id=_AUDIT_ENTITY_ID, campaign_id=None, details=details)

    def create(self, payload: dict, *, actor_id: str) -> list[dict]:
        code = (payload.get("code") or "").strip().upper() if isinstance(payload.get("code"), str) else ""
        if not CODE_PATTERN.match(code):
            raise CampaignException("A code is 3 to 40 capital letters, digits or underscores, starting with a "
                                    "letter (e.g. SALARY_EXPECTATION).", 422, data={"field": "code"})
        label = _label(payload.get("label"))
        sort_order = _sort_order(payload.get("sort_order", self._next_order()))
        if self.db.get(OverrideReasonCode, code) is not None:
            raise CampaignException(f"{code} already exists. Restore it instead of adding it again.", 409,
                                    data={"field": "code"})
        try:
            self.db.add(OverrideReasonCode(code=code, label=label, is_active=True, sort_order=sort_order,
                                           updated_by=actor_id, updated_at=datetime.now(timezone.utc)))
            self.db.flush()
            self._audit(actor_id, {"title": "Override reason code added", "code": code, "sort_order": sort_order})
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.list()

    def _next_order(self) -> int:
        top = self.db.query(func.max(OverrideReasonCode.sort_order)).scalar()
        return min(int(top or 0) + 10, 10000)

    def update(self, code: str, payload: dict, *, actor_id: str) -> list[dict]:
        row = self.db.get(OverrideReasonCode, code)
        if row is None:
            raise CampaignException("Unknown reason code.", 404)
        unknown = set(payload or {}) - {"label", "is_active", "sort_order"}
        if unknown or not payload:
            raise CampaignException("Only label, is_active and sort_order can be changed; a code is never renamed.",
                                    422)
        before = {"label": row.label, "is_active": row.is_active, "sort_order": row.sort_order}
        after = dict(before)
        if "label" in payload:
            after["label"] = _label(payload["label"])
        if "is_active" in payload:
            if not isinstance(payload["is_active"], bool):
                raise CampaignException("is_active must be true or false.", 422, data={"field": "is_active"})
            if code == LOCKED_CODE and payload["is_active"] is False:
                raise CampaignException("OTHER can't be retired: a person must always be able to give a reason.",
                                        422, data={"field": "is_active"})
            after["is_active"] = payload["is_active"]
        if "sort_order" in payload:
            after["sort_order"] = _sort_order(payload["sort_order"])
        changes = audited_changes(before, after, text_fields=("label",))
        if not changes:
            return self.list()
        try:
            row.label, row.is_active, row.sort_order = after["label"], after["is_active"], after["sort_order"]
            row.updated_by, row.updated_at = actor_id, datetime.now(timezone.utc)
            self.db.flush()
            self._audit(actor_id, {"title": "Override reason code changed", "code": code, "changes": changes})
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.list()
