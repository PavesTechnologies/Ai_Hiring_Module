"""
LLM settings on the Settings page (settings phase 4): the daily LLM call caps and the model capability map,
each validated before saving, previewed (before -> after, warnings) and audited with numbers and model names
only. Retries and the embedding batch settings are registered platform settings (General tab); provider
keys and model assignments stay on the AI providers tab; prices on the monitoring panel.

Caps (SCORING_V2_DAILY_LLM_CALL_CAP): {"default": n, "models": {"<model>": n}} - whole numbers 0..100000; a
model must be one the platform uses - assigned on the AI providers tab, or a scoring version's model (a cap for a
misspelled name would silently not apply, and the capability map's catch-all rules match any name). A cap under 30 is
warned about: one candidate's core-skill confirmation takes about 3 calls per skill, and at the cap
candidates are scored without the LLM and held for review.

Capability map (LLM_MODEL_CAPABILITIES): rules {provider, model_pattern, supports_temperature,
structured_output, max_output_tokens, source}; the first matching rule wins. Every model assigned on the AI
providers tab must still match a rule of its provider.
"""
from __future__ import annotations

import fnmatch
import json
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.identity import UserRole
from app.repositories.config_repository import ConfigRepository
from app.services.campaign.scoring_v2_service import DAILY_LLM_CALL_CAP_KEY, daily_llm_call_caps
from app.services.llm.capabilities import (
    LLM_MODEL_CAPABILITIES_KEY, STRUCTURED_OUTPUT_METHODS, load_capability_rules,
)

_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")
CAP_MAX = 100_000
CAP_WARN_BELOW = 30
MAX_RULES = 200


def _matches_a_rule(model: str, rules: list[dict]) -> bool:
    name = (model or "").lower().removeprefix("models/")
    return any(fnmatch.fnmatchcase(name, str(r.get("model_pattern", "")).lower()) for r in rules)


def assigned_models(db) -> list[tuple[str, str]]:
    """(provider, model) for every model assigned on the AI providers tab (main and backup)."""
    from app.models.ai_provider import AIProviderConfig, AIProviderModel
    rows = (db.query(AIProviderConfig.provider, AIProviderModel.model_name, AIProviderModel.fallback_model_name)
            .join(AIProviderModel, AIProviderModel.provider_config_id == AIProviderConfig.id).all())
    out = []
    for provider, main, backup in rows:
        out += [(str(getattr(provider, "value", provider)), m) for m in (main, backup) if m]
    return sorted(set(out))


class LLMSettingsService:
    def __init__(self, db, audit_service):
        self.db = db
        self.config_repository = ConfigRepository(db)
        self.audit_service = audit_service

    def _audit(self, details: dict, actor_id: str) -> None:
        self.audit_service.log(
            actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
            action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
            entity_id=_AUDIT_ENTITY_ID, campaign_id=None, details=details)

    # --- daily caps --------------------------------------------------------------------------------------
    def models_in_use(self) -> list[str]:
        from app.models.scoring_v2 import ScoringVersion
        versions = [m for (m,) in self.db.query(ScoringVersion.model).filter(
            ScoringVersion.model.isnot(None), ScoringVersion.status != "retired").all()]
        return sorted({m for _, m in assigned_models(self.db)} | set(versions))

    def caps(self) -> dict:
        caps = daily_llm_call_caps(self.config_repository)
        return {"default": caps["default"], "models": {k: v for k, v in caps.items() if k != "default"},
                "models_in_use": self.models_in_use()}

    def _validated_caps(self, payload) -> tuple[dict, list[str]]:
        if not isinstance(payload, dict):
            raise CampaignException('Send {"default": n, "models": {"<model>": n}}.', 422)
        problems, warnings = [], []
        in_use = set(self.models_in_use())

        def number(value, where):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= CAP_MAX:
                problems.append(f"{where}: give a whole number from 0 to {CAP_MAX}.")
                return None
            if value < CAP_WARN_BELOW:
                warnings.append(f"{where}: {value} calls a day - candidates beyond it are scored without the LLM "
                                "and held for review.")
            return value

        default = number(payload.get("default"), "Default")
        models = payload.get("models") or {}
        if not isinstance(models, dict):
            problems.append("models: give {\"<model>\": n}.")
            models = {}
        clean = {}
        for model, value in models.items():
            name = str(model).strip()
            if not name or name not in in_use:
                problems.append(f"{name or '(empty)'}: not a model assigned on the AI providers tab or used by a "
                                "scoring version - check the name.")
                continue
            checked = number(value, name)
            if checked is not None:
                clean[name] = checked
        if problems:
            raise CampaignException("The caps can't be saved.", 422, data={"problems": problems})
        return {"default": default, **clean}, warnings

    def preview_caps(self, payload) -> dict:
        after, warnings = self._validated_caps(payload)
        before = daily_llm_call_caps(self.config_repository)
        return {"changes": _number_changes(before, after), "warnings": warnings}

    def save_caps(self, payload, *, actor_id: str) -> dict:
        after, _ = self._validated_caps(payload)
        before = daily_llm_call_caps(self.config_repository)
        changes = _number_changes(before, after)
        if changes:
            try:
                self.config_repository.upsert_configs(
                    {DAILY_LLM_CALL_CAP_KEY: json.dumps(after, sort_keys=True)}, updated_by=actor_id,
                    descriptions={DAILY_LLM_CALL_CAP_KEY: "Maximum core-confirmation LLM calls per day, per model."})
                self._audit({"title": "Daily LLM call caps changed", "changes": changes}, actor_id)
                self.config_repository.commit()
            except Exception:
                self.config_repository.rollback()
                raise
        return {"changed": len(changes), **self.caps()}

    # --- capability map -----------------------------------------------------------------------------------
    def capabilities(self) -> dict:
        return {"rules": load_capability_rules(self.db), "providers": sorted(STRUCTURED_OUTPUT_METHODS),
                "structured_output_methods": {k: list(v) for k, v in STRUCTURED_OUTPUT_METHODS.items()},
                "assigned_models": [{"provider": p, "model": m} for p, m in assigned_models(self.db)]}

    def _validated_rules(self, payload) -> list[dict]:
        rules = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
            raise CampaignException(f'Send {{"rules": [...]}} with 1 to {MAX_RULES} rules.', 422)
        problems, clean = [], []
        for i, rule in enumerate(rules, start=1):
            where = f"Rule {i}"
            if not isinstance(rule, dict):
                problems.append(f"{where}: give an object.")
                continue
            unknown = set(rule) - {"provider", "model_pattern", "supports_temperature", "structured_output",
                                   "max_output_tokens", "source"}
            if unknown:
                problems.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}.")
            provider = str(rule.get("provider") or "").upper()
            if provider not in STRUCTURED_OUTPUT_METHODS:
                problems.append(f"{where}: provider must be one of {', '.join(sorted(STRUCTURED_OUTPUT_METHODS))}.")
                continue
            pattern = rule.get("model_pattern")
            if not isinstance(pattern, str) or not 1 <= len(pattern.strip()) <= 120:
                problems.append(f"{where}: give a model pattern of 1 to 120 characters (e.g. gemini-*).")
            if not isinstance(rule.get("supports_temperature"), bool):
                problems.append(f"{where}: supports_temperature must be true or false.")
            method = rule.get("structured_output")
            if method not in STRUCTURED_OUTPUT_METHODS[provider]:
                problems.append(f"{where}: structured_output for {provider} must be one of "
                                f"{', '.join(STRUCTURED_OUTPUT_METHODS[provider])}.")
            tokens = rule.get("max_output_tokens")
            if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int)
                                       or not 256 <= tokens <= 1_000_000):
                problems.append(f"{where}: max_output_tokens must be empty or a whole number from 256 to 1000000.")
            source = rule.get("source")
            if source is not None and (not isinstance(source, str) or len(source) > 300):
                problems.append(f"{where}: source must be text of up to 300 characters.")
            clean.append({"provider": provider, "model_pattern": (pattern or "").strip(),
                          "supports_temperature": rule.get("supports_temperature"), "structured_output": method,
                          "max_output_tokens": tokens, "source": source or ""})
        for provider, model in assigned_models(self.db):
            if not _matches_a_rule(model, [r for r in clean if r["provider"] == provider]):
                problems.append(f"{model} ({provider}) is assigned on the AI providers tab but no {provider} rule "
                                "matches it.")
        if problems:
            raise CampaignException("The capability map can't be saved.", 422, data={"problems": problems})
        return clean

    def preview_capabilities(self, payload) -> dict:
        after = self._validated_rules(payload)
        return {"changes": _rule_changes(load_capability_rules(self.db), after)}

    def save_capabilities(self, payload, *, actor_id: str) -> dict:
        after = self._validated_rules(payload)
        changes = _rule_changes(load_capability_rules(self.db), after)
        if any(changes.values()):
            try:
                self.config_repository.upsert_configs(
                    {LLM_MODEL_CAPABILITIES_KEY: json.dumps(after, indent=1)}, updated_by=actor_id,
                    descriptions={LLM_MODEL_CAPABILITIES_KEY: "What each model accepts (first matching rule wins)."})
                self._audit({"title": "LLM capability map changed", **changes}, actor_id)
                self.config_repository.commit()
            except Exception:
                self.config_repository.rollback()
                raise
        return {"changed": any(changes.values()), **self.capabilities()}


def _number_changes(before: dict, after: dict) -> dict:
    keys = sorted(set(before) | set(after))
    return {k: {"before": before.get(k), "after": after.get(k)} for k in keys if before.get(k) != after.get(k)}


def _rule_changes(before: list[dict], after: list[dict]) -> dict:
    """Rules by (provider, pattern): added, removed, changed - names and flags only, never the source note."""
    def key(r):
        return f"{str(r.get('provider', '')).upper()}:{r.get('model_pattern')}"

    def comparable(r):
        return {k: r.get(k) for k in ("supports_temperature", "structured_output", "max_output_tokens")}
    old, new = {key(r): r for r in before}, {key(r): r for r in after}
    return {"rules_added": sorted(set(new) - set(old)), "rules_removed": sorted(set(old) - set(new)),
            "rules_changed": sorted(k for k in set(old) & set(new) if comparable(old[k]) != comparable(new[k])),
            "order_changed": [key(r) for r in before if key(r) in new] != [key(r) for r in after if key(r) in old]}
