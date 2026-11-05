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


def test_build_seeds_the_dev_weights_drops_format_only_edits_and_never_the_call_cap_or_scoring_config():
    diff = _diff()
    diff["platform_config"]["change"]['["SCORING_V2_DAILY_LLM_CALL_CAP"]'] = {"value": {"from": "300", "to": "{}"}}
    files = _script("build_seed_data").build(diff)
    approved = files["platform.json"]["sections"][0]
    assert approved["approved"]
    assert sorted(approved["change"]) == ['["CAMPAIGN_WEIGHT_SEMANTIC"]']        # dev weights seeded
    # SCORING_V2_CONFIG isn't read any more (retire v1, phase E): the scoring seed creates the current version
    assert "SCORING_V2_CONFIG" in approved["note"]
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


SENIOR_SKILLS = {"AWS Certified Solutions Architect", "Data Engineering", "Data Governance", "Stakeholder Management"}


def test_every_section_is_reviewed_and_approved_with_the_senior_skills():
    dev_added = set()
    for name, section in _sections():
        assert section["approved"], f"{name}:{section['table']} is not approved"
        if name == "catalogue_dev_added.json":
            assert section["note"].startswith("Reviewed 2026-10-30"), f"{name}:{section['table']} has no review date"
            if section["table"] == "skill_ontology":
                dev_added |= {json.loads(k)[0] for k in section["add"]}
    assert SENIOR_SKILLS <= dev_added                     # added after the 2026-10-26 review, approved 2026-10-30


def test_approve_pending_counts_the_committed_pending_skills_as_reviewed(tmp_path):
    script = _script("build_seed_data")
    committed = tmp_path / "catalogue_dev_added.json"
    committed.write_text(json.dumps({"sections": [
        {"table": "skill_ontology", "approved": True, "add": {'["REST APIs"]': {}}, "change": {'["git"]': {}}},
        {"table": "skill_ontology", "approved": False, "add": {'["Program Management"]': {}}, "change": {}},
    ]}), encoding="utf-8")
    assert script.reviewed_skill_names(str(committed)) == {"rest apis", "git"}
    names = script.reviewed_skill_names(str(committed), include_pending=True)
    assert names == {"rest apis", "git", "program management"}
    diff = _diff()
    diff["skill_ontology"]["add"]['["Data Governance"]'] = {"canonical_name": "Data Governance", "source": "manual entry"}
    sections = script.build(diff, reviewed="2026-10-30", reviewed_names=names)["catalogue_dev_added.json"]["sections"]
    approved = [s for s in sections if s["approved"] and s["table"] == "skill_ontology"][0]
    held = [s for s in sections if not s["approved"] and s["table"] == "skill_ontology"][0]
    assert '["Program Management"]' in approved["add"]                  # was pending, now reviewed
    assert '["Data Governance"]' in held["add"]                         # never in the committed file: still waits


def test_a_regeneration_keeps_reviewed_skills_approved_and_holds_new_ones():
    build = _script("build_seed_data").build
    diff = _diff()
    diff["skill_ontology"]["add"]['["Data Governance"]'] = {"canonical_name": "Data Governance", "source": "manual entry"}
    files = build(diff, reviewed="2026-10-26", reviewed_names={"rest apis", "program management", "git"})
    sections = files["catalogue_dev_added.json"]["sections"]
    approved = [sec for sec in sections if sec["approved"] and sec["table"] == "skill_ontology"][0]
    held = [sec for sec in sections if not sec["approved"] and sec["table"] == "skill_ontology"][0]
    assert '["Data Governance"]' in held["add"] and '["Data Governance"]' not in approved["add"]
    assert '["REST APIs"]' in approved["add"]


def test_a_regeneration_approves_dev_added_skills_only_with_a_review_date():
    build = _script("build_seed_data").build
    unreviewed = build(_diff())["catalogue_dev_added.json"]["sections"]
    assert not any(sec["approved"] for sec in unreviewed)
    reviewed = build(_diff(), reviewed="2026-10-26")["catalogue_dev_added.json"]["sections"]
    assert all(sec["approved"] for sec in reviewed) and reviewed[0]["note"].startswith("Reviewed 2026-10-26")


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
        undated = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "", text)          # review dates are not phone numbers
        assert not contact.search(undated), f"{path.name}: contact-like string"
        assert not uuid.search(text), f"{path.name}: a database id - natural keys only"
        assert not secret.search(text), f"{path.name}: a credential-like key"


def test_dev_added_skills_merged_or_dropped_in_dev_are_not_seeded():
    diff = _diff()
    diff["skill_ontology"]["add"]['["Crew AI"]'] = {"canonical_name": "Crew AI", "source": "manual entry", "is_active": False}
    files = _script("build_seed_data").build(diff)
    pending = files["catalogue_dev_added.json"]["sections"][0]
    assert '["Crew AI"]' not in pending["add"] and "Crew AI" in pending["note"]
    assert '["Crew AI"]' not in files["catalogue.json"]["sections"][0]["add"]


def test_the_committed_data_seeds_no_inactive_dev_added_skill():
    for name, section in _sections():
        if section["table"] == "skill_ontology":
            for key, values in section["add"].items():
                if values.get("source") in ("manual entry", "HR_CALIBRATION"):
                    assert values.get("is_active") is not False, f"{name}: {key} is inactive in dev"


def test_a_catalogue_skill_merged_away_in_dev_is_not_seeded():
    """SNS merged into AWS SNS (inactive in dev, 2026-11-05): like a merged dev-added skill, nothing to seed."""
    diff = _diff()
    diff["skill_ontology"]["add"]['["SNS"]'] = {"canonical_name": "SNS", "source": "seed import", "is_active": False}
    catalogue = _script("build_seed_data").build(diff)["catalogue.json"]["sections"][0]
    assert list(catalogue["add"]) == ['["Kafka"]'] and "['SNS']" in catalogue["note"]


def test_a_stale_dev_description_is_not_seeded_but_a_value_change_still_is():
    diff = _diff()
    stale = {"description": {"from": "the current text", "to": "dev's older text"}}
    diff["platform_config"]["change"]['["OVERRIDE_RATE_ALERT_THRESHOLD"]'] = stale
    approved = _script("build_seed_data").build(diff)["platform.json"]["sections"][0]
    assert '["OVERRIDE_RATE_ALERT_THRESHOLD"]' not in approved["change"]
    assert "Stale dev descriptions not seeded: ['OVERRIDE_RATE_ALERT_THRESHOLD']" in approved["note"]
    diff["platform_config"]["change"]['["OVERRIDE_RATE_ALERT_THRESHOLD"]'] = {"value": {"from": "20", "to": "25"}}
    assert '["OVERRIDE_RATE_ALERT_THRESHOLD"]' in _script("build_seed_data").build(diff)["platform.json"]["sections"][0]["change"]
