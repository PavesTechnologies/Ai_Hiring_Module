"""Read, preview and save the registered platform settings (see platform_settings.REGISTRY). HR_ADMIN only."""
from uuid import UUID

from app.enums.constants import ActionType, EntityType, UserRole
from app.repositories.config_repository import ConfigRepository
from app.services.settings.audit_values import audited_changes
from app.services.settings.platform_settings import (
    BY_KEY, GROUP_ORDER, REGISTRY, parse_stored, stored_text, validate, warnings_for,
)

PLATFORM_CONFIG_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")


class PlatformSettingsService:
    def __init__(self, config_repository: ConfigRepository, audit_service):
        self.config_repository = config_repository
        self.audit_service = audit_service

    def _effective(self) -> dict:
        stored = self.config_repository.get_configs_by_keys([s.key for s in REGISTRY])
        values = {}
        for setting in REGISTRY:
            parsed = parse_stored(setting, stored.get(setting.key))
            values[setting.key] = (parsed if parsed is not None else setting.default, parsed is None)
        return values

    def list(self) -> list[dict]:
        values = self._effective()
        groups = []
        for group in GROUP_ORDER:
            items = []
            for s in (s for s in REGISTRY if s.group == group):
                value, is_default = values[s.key]
                items.append({"key": s.key, "label": s.label, "help": s.help, "kind": s.kind,
                              "minimum": s.minimum, "maximum": s.maximum, "default": s.default, "unit": s.unit,
                              "value": value, "is_default": is_default})
            groups.append({"group": group, "settings": items})
        return groups

    def preview(self, values: dict) -> dict:
        """What saving would change (before -> after), and any warnings. Saves nothing."""
        clean = validate(values)
        current = {k: v for k, (v, _) in self._effective().items()}
        changes = [{"key": k, "label": BY_KEY[k].label, "unit": BY_KEY[k].unit, "before": current[k], "after": v}
                   for k, v in clean.items() if current[k] != v]
        return {"changes": changes, "warnings": warnings_for(clean)}

    def save(self, values: dict, *, actor_id: str) -> dict:
        clean = validate(values)
        current = {k: v for k, (v, _) in self._effective().items()}
        changed = {k: v for k, v in clean.items() if current[k] != v}
        if not changed:
            return {"changed": [], "settings": self.list()}
        try:
            self.config_repository.upsert_configs(
                {k: stored_text(BY_KEY[k], v) for k, v in changed.items()}, updated_by=actor_id,
                descriptions={k: BY_KEY[k].help for k in changed})
            self.audit_service.log(
                actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
                action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
                entity_id=PLATFORM_CONFIG_AUDIT_ENTITY_ID, campaign_id=None,
                details={"title": "Platform settings updated",
                         "changes": audited_changes({k: current[k] for k in changed}, changed)},
            )
            self.config_repository.commit()
        except Exception:
            self.config_repository.rollback()
            raise
        return {"changed": sorted(changed), "settings": self.list()}
