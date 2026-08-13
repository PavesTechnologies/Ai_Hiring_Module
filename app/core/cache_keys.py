import hashlib
import json
from typing import Any

from app.core.config import settings

PREFIX = settings.cache_key_prefix


def _hash_params(params: dict[str, Any]) -> str:
    normalized = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --- JD ---

def jd_key(jd_id: Any) -> str:
    return f"{PREFIX}:jd:{jd_id}"


def jd_list_key(is_active_version: bool) -> str:
    return f"{PREFIX}:jd:list:{is_active_version}"


def jd_list_prefix() -> str:
    return f"{PREFIX}:jd:list:"


def jd_search_key(params: dict[str, Any]) -> str:
    return f"{PREFIX}:jd:search:{_hash_params(params)}"


def jd_search_prefix() -> str:
    return f"{PREFIX}:jd:search:"


def jd_processing_status_key(jd_id: Any) -> str:
    return f"{PREFIX}:jd:{jd_id}:processing:status"


# --- Resume / Candidate ---

def resume_key(resume_id: Any) -> str:
    return f"{PREFIX}:resume:{resume_id}"


def candidate_key(candidate_id: Any) -> str:
    return f"{PREFIX}:candidate:{candidate_id}"


def resume_list_key(params: dict[str, Any]) -> str:
    return f"{PREFIX}:resume:list:{_hash_params(params)}"


def resume_list_prefix() -> str:
    return f"{PREFIX}:resume:list:"


def candidate_list_key(params: dict[str, Any]) -> str:
    return f"{PREFIX}:candidate:list:{_hash_params(params)}"


def candidate_list_prefix() -> str:
    return f"{PREFIX}:candidate:list:"


def resume_processing_status_key(resume_id: Any) -> str:
    return f"{PREFIX}:resume:{resume_id}:processing:status"


# --- Campaign ---

def campaign_key(campaign_id: Any) -> str:
    return f"{PREFIX}:campaign:{campaign_id}"


def campaign_list_key(params: dict[str, Any]) -> str:
    return f"{PREFIX}:campaign:list:{_hash_params(params)}"


def campaign_list_prefix() -> str:
    return f"{PREFIX}:campaign:list:"


def campaign_scoring_key(campaign_id: Any) -> str:
    return f"{PREFIX}:campaign:{campaign_id}:scoring"


def campaign_platform_defaults_key() -> str:
    return f"{PREFIX}:campaign:platform-defaults"


def campaign_weight_presets_key(org_id: Any) -> str:
    return f"{PREFIX}:campaign:weight-presets:{org_id}"


# --- Skill ontology ---

def skill_key(skill_id: Any) -> str:
    return f"{PREFIX}:skill:{skill_id}"


def skill_name_key(normalized_name: str) -> str:
    return f"{PREFIX}:skill:name:{normalized_name.strip().lower()}"


def skill_alias_key(alias: str) -> str:
    return f"{PREFIX}:skill:alias:{alias.strip().lower()}"


def skill_catalog_key() -> str:
    return f"{PREFIX}:skill:catalog:canonical-names"


def skill_alias_catalog_key() -> str:
    return f"{PREFIX}:skill:catalog:aliases"


def skill_categories_key() -> str:
    return f"{PREFIX}:skill:categories"


def skill_dashboard_summary_key() -> str:
    return f"{PREFIX}:skill:dashboard-summary"


def skill_prefix() -> str:
    return f"{PREFIX}:skill:"


# --- Dashboard / monitoring ---

def dashboard_key(kind: str, params: dict[str, Any]) -> str:
    return f"{PREFIX}:dashboard:{kind}:{_hash_params(params)}"


def dashboard_prefix() -> str:
    return f"{PREFIX}:dashboard:"


# --- Reference / config data ---

def reference_key(name: str) -> str:
    return f"{PREFIX}:reference:{name}"


# --- Processing pipeline ---

def processing_status_key(kind: str, document_id: Any) -> str:
    return f"{PREFIX}:processing:{kind}:{document_id}:status"


# --- Stampede lock ---

def lock_key(cache_key: str) -> str:
    return f"{PREFIX}:lock:{cache_key}"
