"""
Stage transitions on the Settings page (settings phase 2). HR admins change which roles may make each move and
whether it needs a reason, add moves and remove moves - as one batch, checked against transition_rules on the
table as it would be afterwards. A batch that breaks any rule is refused whole, every problem listed.

Changes (a list):
  {"op": "update", "id": ..., "allowed_roles": [...], "requires_reason": bool}   (either field optional)
  {"op": "add", "previous_stage": null | stage, "from_stage": ..., "to_stage": ..., "allowed_roles": [...],
   "requires_reason": bool}
  {"op": "remove", "id": ...}
SYSTEM is never added or removed here: a row's SYSTEM role is kept as it is.
The audit records the moves (stages), roles and flags before/after - no free text.
"""
from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.identity import UserRole
from app.models.pipeline import AllowedTransition, PipelineStage
from app.services.settings.transition_rules import (
    HUMAN_ROLES, ROUTES_FOR_ROLE, SYSTEM, TERMINAL_STAGES, describe, protected_reason, violations,
)

_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")


def _row(r: AllowedTransition) -> dict:
    return {"id": str(r.id), "previous_stage": r.previous_stage, "from_stage": r.from_stage, "to_stage": r.to_stage,
            "allowed_roles": list(r.allowed_roles or []), "requires_reason": bool(r.requires_reason)}


def _stage(value, field: str, *, optional=False):
    if value is None and optional:
        return None
    try:
        return PipelineStage(value)
    except (ValueError, TypeError):
        raise CampaignException(f"{field}: unknown stage.", 422)


def _human_roles(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise CampaignException("allowed_roles must be a list of roles.", 422)
    if SYSTEM in value:
        raise CampaignException("SYSTEM can't be granted or removed here: the automated moves are set in code.", 422)
    return [r for r in HUMAN_ROLES if r in value] + [r for r in value if r not in HUMAN_ROLES]


def _out(row: dict) -> dict:
    return {**row, "previous_stage": row["previous_stage"].value if row["previous_stage"] else None,
            "from_stage": row["from_stage"].value, "to_stage": row["to_stage"].value}


class TransitionSettingsService:
    def __init__(self, db, audit_service):
        self.db = db
        self.audit_service = audit_service

    def _current(self) -> list[dict]:
        rows = self.db.query(AllowedTransition).all()
        return sorted((_row(r) for r in rows),
                      key=lambda r: (list(PipelineStage).index(r["from_stage"]),
                                     list(PipelineStage).index(r["to_stage"]),
                                     -1 if r["previous_stage"] is None else list(PipelineStage).index(r["previous_stage"])))

    def list(self) -> dict:
        rows = self._current()
        return {
            "stages": [s.value for s in PipelineStage],
            "terminal_stages": sorted(s.value for s in TERMINAL_STAGES),
            "roles": list(HUMAN_ROLES),
            "routes_for_role": {role: sorted(routes) for role, routes in ROUTES_FOR_ROLE.items()},
            "transitions": [{**_out(r), "protected": protected_reason(r), "system": SYSTEM in r["allowed_roles"]}
                            for r in rows],
        }

    def _apply(self, changes) -> tuple[list[dict], list[dict], set]:
        """(rows after, audit entries, keys added). Raises 422 on malformed changes or broken rules."""
        if not isinstance(changes, list) or not changes:
            raise CampaignException("Send at least one change.", 422)
        rows = {r["id"]: dict(r) for r in self._current()}
        entries, added = [], set()
        for change in changes:
            op = change.get("op") if isinstance(change, dict) else None
            if op == "update":
                row = rows.get(str(change.get("id")))
                if row is None:
                    raise CampaignException("A move to change no longer exists. Reload and try again.", 409)
                entry = {"op": "update", "move": describe(row)}
                if "allowed_roles" in change:
                    system = [SYSTEM] if SYSTEM in row["allowed_roles"] else []
                    roles = _human_roles(change["allowed_roles"]) + system
                    if roles != row["allowed_roles"]:
                        entry["allowed_roles"] = {"before": row["allowed_roles"], "after": roles}
                        row["allowed_roles"] = roles
                if "requires_reason" in change:
                    if not isinstance(change["requires_reason"], bool):
                        raise CampaignException("requires_reason must be true or false.", 422)
                    if change["requires_reason"] != row["requires_reason"]:
                        entry["requires_reason"] = {"before": row["requires_reason"], "after": change["requires_reason"]}
                        row["requires_reason"] = change["requires_reason"]
                if len(entry) > 2:
                    entries.append(entry)
            elif op == "add":
                row = {"id": f"new-{uuid.uuid4()}",
                       "previous_stage": _stage(change.get("previous_stage"), "previous_stage", optional=True),
                       "from_stage": _stage(change.get("from_stage"), "from_stage"),
                       "to_stage": _stage(change.get("to_stage"), "to_stage"),
                       "allowed_roles": _human_roles(change.get("allowed_roles") or []),
                       "requires_reason": change.get("requires_reason", False)}
                if not isinstance(row["requires_reason"], bool):
                    raise CampaignException("requires_reason must be true or false.", 422)
                if row["from_stage"] == row["to_stage"] and row["previous_stage"] is None:
                    raise CampaignException("A move needs two different stages.", 422)
                rows[row["id"]] = row
                added.add((row["previous_stage"], row["from_stage"], row["to_stage"]))
                entries.append({"op": "add", "move": describe(row), "allowed_roles": row["allowed_roles"],
                                "requires_reason": row["requires_reason"]})
            elif op == "remove":
                row = rows.get(str(change.get("id")))
                if row is None:
                    raise CampaignException("A move to remove no longer exists. Reload and try again.", 409)
                reason = protected_reason(row)
                if reason:
                    raise CampaignException(f"{describe(row)} can't be removed: {reason}", 422,
                                            data={"problems": [f"{describe(row)} can't be removed: {reason}"]})
                del rows[row["id"]]
                entries.append({"op": "remove", "move": describe(row), "allowed_roles": row["allowed_roles"]})
            else:
                raise CampaignException('Each change needs "op": "update", "add" or "remove".', 422)
        after = list(rows.values())
        problems = violations(after, added_keys=added)
        if problems:
            raise CampaignException("These changes would break the pipeline rules.", 422, data={"problems": problems})
        return after, entries, added

    def preview(self, changes) -> dict:
        """What the batch changes, checked against every rule. Saves nothing."""
        _, entries, _ = self._apply(changes)
        return {"changes": entries}

    def save(self, changes, *, actor_id: str) -> dict:
        after, entries, _ = self._apply(changes)
        if not entries:
            return {"changed": 0, **self.list()}
        try:
            current = {str(r.id): r for r in self.db.query(AllowedTransition).all()}
            keep = {r["id"] for r in after}
            for row_id, model in current.items():
                if row_id not in keep:
                    self.db.delete(model)
            self.db.flush()
            for row in after:
                if row["id"].startswith("new-"):
                    self.db.add(AllowedTransition(id=uuid.uuid4(), previous_stage=row["previous_stage"],
                                                  from_stage=row["from_stage"], to_stage=row["to_stage"],
                                                  allowed_roles=row["allowed_roles"],
                                                  requires_reason=row["requires_reason"]))
                else:
                    model = current[row["id"]]
                    model.allowed_roles = row["allowed_roles"]
                    model.requires_reason = row["requires_reason"]
            self.db.flush()
            self.audit_service.log(
                actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
                action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
                entity_id=_AUDIT_ENTITY_ID, campaign_id=None,
                details={"title": "Stage transitions changed", "changes": entries})
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise CampaignException("Someone else changed the moves at the same time. Reload and try again.", 409)
        except Exception:
            self.db.rollback()
            raise
        return {"changed": len(entries), **self.list()}
