"""
The product-configuration seed (app/seeds/seed_product_config.py), its data
files (app/seeds/product_config/) and the tools that build them
(scripts/config_seed/): the planning rules that make it idempotent and keep
edits, the approved / pending split, and that the data holds configuration
only - no database ids, contact details or credentials.

Pure functions and files only; the end-to-end check (fresh database ->
migrations -> seeds -> this seed -> diff against dev) was run against a
throwaway local database (see docs/roadmap.md).
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.seeds import seed_product_config as seed

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "app" / "seeds" / "product_config"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "config_seed" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- planning ---------------------------------------------------------------------------------

def test_add_inserts_a_missing_row_and_leaves_an_existing_one():
    fields = ["category", "description"]
    assert seed.plan_add(None, {"category": "Backend"}, fields) == ("insert", {"category": "Backend"})
    assert seed.plan_add({"category": "Backend", "description": None}, {"category": "Backend", "description": None},
                         fields) == ("noop", {})
    action, detail = seed.plan_add({"category": "Data", "description": None}, {"category": "Backend"}, fields)
    assert action == "exists" and detail == {"category": "Backend"}


def test_change_only_moves_a_field_still_at_its_seeded_value():
    change = {"value": {"from": "300", "to": "400"}, "description": {"from": "a", "to": "b"}}
    assert seed.plan_change({"value": "300", "description": "a"}, change) == \
        ("update", {"value": "400", "description": "b"})
    assert seed.plan_change({"value": "400", "description": "b"}, change) == ("noop", {})          # done before
    action, detail = seed.plan_change({"value": "250", "description": "a"}, change)               # edited here
    assert action == "edited" and detail == {"fields": ["value"], "updates": {"description": "b"}}
    assert seed.plan_change(None, change) == ("missing", {})


def test_a_parent_that_does_not_exist_yet_waits():
    values, waiting = seed.without_unresolvable_parent({"canonical_name": "MySQL", "parent": "sql"}, {"MySQL"})
    assert waiting == "sql" and "parent" not in values
    values, waiting = seed.without_unresolvable_parent({"canonical_name": "MySQL", "parent": "sql"}, {"MySQL", "sql"})
    assert waiting is None and values["parent"] == "sql"


def test_sections_load_in_dependency_order_and_unknown_files_are_refused(tmp_path):
    for name in ("platform.json", "catalogue.json"):
        (tmp_path / name).write_text(json.dumps({"sections": [{"table": name}]}), encoding="utf-8")
    assert [n for n, _ in seed.load_sections(str(tmp_path))] == ["catalogue.json", "platform.json"]
    (tmp_path / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        seed.load_sections(str(tmp_path))


# --- building the data from a diff -----------------------------------------------------------

def _diff():
    skill_fields = ["aliases", "category", "parent", "description", "not_description", "is_active", "source",
                    "confidence"]
    empty = {"keys": ["k"], "fields": [], "add": {}, "change": {}, "only_in_reference": []}
    return {
        "skill_ontology": {"keys": ["canonical_name"], "fields": skill_fields, "only_in_reference": [],
                           "add": {'["Kafka"]': {"canonical_name": "Kafka", "source": "seed import"},
                                   '["REST APIs"]': {"canonical_name": "REST APIs", "source": "manual entry"},
                                   '["Program Management"]': {"canonical_name": "Program Management",
                                                              "source": "manual entry"}},
                           "change": {'["git"]': {"source": {"from": "SEED_STOPGAP", "to": "manual entry"}},
                                      '["Python"]': {"aliases": {"from": None, "to": ["py"]}}}},
        "skill_senses": {**empty, "add": {'["Python", "x"]': {"skill": "Python", "sense_key": "x"}}},
        "skill_pattern_rules": {**empty, "add": {'["PM title"]': {"name": "PM title", "skill": "Program Management"}}},
        "certification_skills": {**empty},
        "platform_config": {"keys": ["key"], "fields": ["value", "description"], "add": {}, "only_in_reference": [],
                            "change": {'["AI_PASS_THRESHOLD"]': {"value": {"from": "50.00", "to": "50"}},
                                       '["CAMPAIGN_WEIGHT_SEMANTIC"]': {"value": {"from": "40.00", "to": "30"}},
                                       '["SCORING_V2_CONFIG"]': {"value": {"from": "{}", "to": "{\"a\": 1}"}}}},
        "prompt_templates": {**empty}, "embedding_model_versions": {**empty},
    }


def test_build_splits_dev_added_skills_and_their_rules_into_the_pending_review_file():
    files = _script("build_seed_data").build(_diff())
    catalogue = files["catalogue.json"]["sections"][0]
    assert catalogue["approved"] and list(catalogue["add"]) == ['["Kafka"]'] and list(catalogue["change"]) == ['["Python"]']
    pending = {s["table"]: s for s in files["catalogue_dev_added.json"]["sections"]}
    assert not any(s["approved"] for s in pending.values())
    assert set(pending["skill_ontology"]["add"]) == {'["REST APIs"]', '["Program Management"]'}
    assert list(pending["skill_ontology"]["change"]) == ['["git"]']
    assert pending["skill_ontology"]["renames"] == {"REST API": "REST APIs"}     # travels with its target
    assert list(pending["skill_pattern_rules"]["add"]) == ['["PM title"]']        # names a pending skill
    rules = {s["table"]: s for s in files["scoring_rules.json"]["sections"]}
    assert list(rules["skill_senses"]["add"]) == ['["Python", "x"]']             # names an approved skill


def test_build_seeds_the_dev_weights_drops_format_only_edits_and_never_the_call_cap():
    diff = _diff()
    diff["platform_config"]["change"]['["SCORING_V2_DAILY_LLM_CALL_CAP"]'] = {"value": {"from": "300", "to": "{}"}}
    files = _script("build_seed_data").build(diff)
    approved = files["platform.json"]["sections"][0]
    assert approved["approved"]
    assert sorted(approved["change"]) == ['["CAMPAIGN_WEIGHT_SEMANTIC"]', '["SCORING_V2_CONFIG"]']   # dev weights seeded
    assert "AI_PASS_THRESHOLD" in approved["note"]                               # 50.00 -> 50 is the same number
    assert "platform_pending_decision.json" not in files
    assert not any('"SCORING_V2_DAILY_LLM_CALL_CAP"' in key for f in files.values()
                   for sec in f["sections"] for key in list(sec["add"]) + list(sec["change"]))


def test_diff_reports_adds_changes_and_rows_only_in_the_reference():
    diff = _script("diff_config").diff
    reference = {"t": {"keys": ["k"], "fields": ["v"], "rows": {'["a"]': {"k": "a", "v": 1}, '["gone"]': {"k": "gone", "v": 0}}}}
    source = {"t": {"keys": ["k"], "fields": ["v"], "rows": {'["a"]': {"k": "a", "v": 2}, '["new"]': {"k": "new", "v": 3}}}}
    result = diff(reference, source)["t"]
    assert list(result["add"]) == ['["new"]']
    assert result["change"] == {'["a"]': {"v": {"from": 1, "to": 2}}}
    assert result["only_in_reference"] == ['["gone"]']


# --- the committed data files -------------------------------------------------------------------

def _sections():
    return seed.load_sections(str(DATA))


def test_the_pending_files_stay_unapproved_until_reviewed():
    for name, section in _sections():
        if name == "catalogue_dev_added.json":
            assert section["approved"] is False, f"{name}:{section['table']} must stay unapproved until reviewed"


def test_approved_rules_only_name_skills_an_approved_section_or_the_base_seed_provides():
    approved_skills = {json.loads(k)[0] for n, s in _sections()
                       if s["table"] == "skill_ontology" and s["approved"] for k in s["add"]}
    stopgap = re.findall(r'"canonical_name":\s*"([^"]+)"|\(\s*"([^"]+)"',
                         (ROOT / "app" / "seeds" / "seed_skill_ontology_stopgap.py").read_text(encoding="utf-8"))
    known = approved_skills | {a or b for a, b in stopgap}
    for name, section in _sections():
        if section["approved"] and section["table"] in ("skill_senses", "skill_pattern_rules", "certification_skills"):
            for values in section["add"].values():
                assert values["skill"] in known, f"{name}:{section['table']} names {values['skill']!r}"


def test_the_daily_llm_call_cap_is_never_seeded():
    for name, section in _sections():
        keys = list(section["add"]) + list(section["change"])
        assert '["SCORING_V2_DAILY_LLM_CALL_CAP"]' not in keys, f"{name}: set it in production after enabling billing"


def test_the_dev_default_weights_are_seeded():
    platform = next(s for n, s in _sections() if n == "platform.json" and s["table"] == "platform_config")
    assert platform["approved"]
    assert platform["change"]['["CAMPAIGN_WEIGHT_DETERMINISTIC"]']["value"]["to"] == "40"
    assert platform["change"]['["CAMPAIGN_WEIGHT_SEMANTIC"]']["value"]["to"] == "30"


def test_the_data_is_configuration_only():
    contact = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}|\+?\d[\d\s().-]{8,}\d", re.I)
    uuid = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
    secret = re.compile(r"(api[_-]?key|secret|password|token)\"\s*:", re.I)
    for path in DATA.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not contact.search(text), f"{path.name}: contact-like string"
        assert not uuid.search(text), f"{path.name}: a database id - natural keys only"
        assert not secret.search(text), f"{path.name}: a credential-like key"
