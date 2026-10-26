"""
The product-configuration tables, their natural keys and value fields, and the
SELECT that reads each in canonical form (ids resolved to names). Shared by
scripts/config_seed/export_config.py and app/seeds/seed_product_config.py.
"""
import json

# table -> natural-key fields, value fields, the SELECT producing them (ids resolved to names)
TABLES = {
    "skill_ontology": (
        ["canonical_name"],
        ["aliases", "category", "parent", "description", "not_description", "is_active", "source", "confidence"],
        """select s.canonical_name, s.aliases, s.category, p.canonical_name as parent, s.description,
                  s.not_description, s.is_active, s.source, s.confidence
           from skill_ontology s left join skill_ontology p on p.id = s.parent_skill_id"""),
    "skill_scan_terms": (
        ["term_key"], ["policy", "cue_words", "note"],
        "select term_key, policy::text as policy, cue_words, note from skill_scan_terms"),
    "skill_senses": (
        ["skill", "sense_key"], ["label", "description", "cue_words", "is_default", "window_lines", "is_active"],
        """select s.canonical_name as skill, x.sense_key, x.label, x.description, x.cue_words, x.is_default,
                  x.window_lines, x.is_active
           from skill_senses x join skill_ontology s on s.id = x.canonical_skill_id"""),
    "skill_pattern_rules": (
        ["name"], ["skill", "anchor_terms", "near_terms", "max_gap_words", "sections", "anchor_case_sensitive",
                   "is_active", "note", "match_scope"],
        """select r.name, s.canonical_name as skill, r.anchor_terms, r.near_terms, r.max_gap_words, r.sections,
                  r.anchor_case_sensitive, r.is_active, r.note, r.match_scope::text as match_scope
           from skill_pattern_rules r join skill_ontology s on s.id = r.canonical_skill_id"""),
    "certifications": (
        ["name"], ["name_variants", "note"],
        "select name, name_variants, note from certifications"),
    "certification_skills": (
        ["certification", "skill"], ["sense_key"],
        """select c.name as certification, s.canonical_name as skill, x.sense_key
           from certification_skills x join certifications c on c.id = x.certification_id
           join skill_ontology s on s.id = x.canonical_skill_id"""),
    "education_field_relations": (
        ["field_a", "field_b", "role_domain"], ["note"],
        "select field_a, field_b, role_domain, note from education_field_relations"),
    "jd_profile_templates": (
        ["key"], ["label", "description", "internship_credit_pct", "experience_tolerance_years",
                  "education_requirement", "evidence_sections", "listed_only_can_confirm"],
        """select key, label, description, internship_credit_pct, experience_tolerance_years,
                  education_requirement, evidence_sections, listed_only_can_confirm from jd_profile_templates"""),
    "platform_config": (
        ["key"], ["value", "description"],
        "select key, value, description from platform_config"),
    "prompt_templates": (
        ["task_type", "name"], ["template_text", "content_hash", "status", "notes"],
        """select task_type::text as task_type, name, template_text, content_hash, status::text as status, notes
           from prompt_templates"""),
    "email_templates": (
        ["trigger_event", "name"], ["subject", "body_template", "is_active"],
        "select trigger_event::text as trigger_event, name, subject, body_template, is_active from email_templates"),
    "allowed_transitions": (
        ["previous_stage", "from_stage", "to_stage"], ["allowed_roles", "requires_reason", "notes"],
        """select previous_stage::text as previous_stage, from_stage::text as from_stage, to_stage::text as to_stage,
                  allowed_roles, requires_reason, notes from allowed_transitions"""),
    "embedding_model_versions": (
        ["model_name", "model_version"], ["vector_dimensions", "distance_metric", "is_active", "notes"],
        """select model_name, model_version, vector_dimensions, distance_metric::text as distance_metric,
                  is_active, notes from embedding_model_versions"""),
    "campaign_weight_presets": (
        ["name"], ["description", "weight_deterministic", "weight_semantic", "weight_ai",
                   "deterministic_threshold", "semantic_threshold", "ai_threshold"],
        """select name, description, weight_deterministic, weight_semantic, weight_ai, deterministic_threshold,
                  semantic_threshold, ai_threshold from campaign_weight_presets"""),
    "retention_policies": (
        ["entity_type", "jurisdiction"], ["retention_days", "is_active", "policy_version"],
        """select entity_type::text as entity_type, jurisdiction::text as jurisdiction, retention_days, is_active,
                  policy_version from retention_policies"""),
}



def plain(value):
    """JSON-stable values: Decimals as strings, enums as their value, everything else as is."""
    if value is None or isinstance(value, (str, int, bool, float, list, dict)):
        return value
    if hasattr(value, "value"):
        return value.value
    return str(value)


def row_key(row: dict, keys: list[str]) -> str:
    return json.dumps([plain(row[k]) for k in keys], ensure_ascii=False)
