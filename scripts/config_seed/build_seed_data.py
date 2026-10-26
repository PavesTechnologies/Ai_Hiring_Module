"""
Turn a reviewed diff (diff_config.py: dev vs a fresh database) into the seed
data files app/seeds/seed_product_config.py loads:

    python scripts/config_seed/build_seed_data.py <diff.json> [out dir] [--reviewed YYYY-MM-DD]

--reviewed: the dev-added skills (and the rules naming them) were reviewed on that
date - their sections are written approved, with the date in the note. Without
it they are written unapproved; a regeneration never approves anything by itself.

Each file is one section with an "approved" flag; the seed loads approved
sections only. catalogue_dev_added.json is written unapproved, by decision:
skills added in dev by hand or by JD calibration (production checklist: review
them before export - docs/seed_review_dev_added_skills.md), and any rule that
needs one of them. The dev default weights are seeded (decided 2026-10-26).
Never seeded: SCORING_V2_DAILY_LLM_CALL_CAP - production sets the per-model
daily cap after enabling billing (docs/roadmap.md). Format-only setting edits
(30.00 -> 30) are dropped as no-ops.
"""
import json
import os
import sys
from decimal import Decimal, InvalidOperation

DEV_ADDED_SOURCES = {"manual entry", "HR_CALIBRATION"}
# Never seeded: environment-specific (dev runs on the free tier). Production sets it after enabling billing.
EXCLUDED_SETTINGS = {"SCORING_V2_DAILY_LLM_CALL_CAP"}
# Renamed in dev; applied to a fresh database only while the old row is untouched.
RENAMES = {"skill_ontology": {"REST API": "REST APIs"}}


def _same_number(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, TypeError):
        return False


def _section(table, keys, fields, *, approved, note, add=None, change=None, renames=None):
    return {"table": table, "keys": keys, "fields": fields, "approved": approved, "note": note,
            "renames": renames or {}, "add": add or {}, "change": change or {}}


def build(diff: dict, reviewed: str | None = None) -> dict:
    files = {}
    skills = diff["skill_ontology"]
    dev_added_all = {k: v for k, v in skills["add"].items() if v.get("source") in DEV_ADDED_SOURCES}
    # merged or dropped in dev (deactivated): nothing to seed into a fresh database
    retired = sorted(json.loads(k)[0] for k, v in dev_added_all.items() if v.get("is_active") is False)
    dev_added = {k: v for k, v in dev_added_all.items() if v.get("is_active") is not False}
    base_add = {k: v for k, v in skills["add"].items() if k not in dev_added_all}
    dev_changed = {k: v for k, v in skills["change"].items() if (v.get("source") or {}).get("to") in DEV_ADDED_SOURCES}
    base_change = {k: v for k, v in skills["change"].items() if k not in dev_changed}
    pending_skill_names = {json.loads(k)[0] for k in list(dev_added) + list(dev_changed)}
    # a rename travels with the row it renames to
    renames = RENAMES["skill_ontology"]
    pending_renames = {old: new for old, new in renames.items() if new in pending_skill_names}
    base_renames = {old: new for old, new in renames.items() if old not in pending_renames}

    files["catalogue.json"] = _section(
        "skill_ontology", skills["keys"], skills["fields"], approved=True,
        note="The skill catalogue: bulk-imported and seeded skills and corrections to the stop-gap seed "
             "rows. Embeddings are not seeded - run scripts/generate_missing_skill_embeddings.py after loading.",
        add=base_add, change=base_change, renames=base_renames)

    rules_approved, rules_pending = {}, {}
    for table in ("skill_senses", "skill_pattern_rules", "certification_skills"):
        t = diff[table]
        ok = {k: v for k, v in t["add"].items() if v.get("skill") not in pending_skill_names}
        held = {k: v for k, v in t["add"].items() if k not in ok}
        rules_approved[table] = _section(table, t["keys"], t["fields"], approved=True,
                                         note="Needs the catalogue loaded first.", add=ok)
        if held:
            rules_pending[table] = _section(table, t["keys"], t["fields"], approved=False,
                                            note="Waits for the dev-added skill it names.", add=held)
    files["scoring_rules.json"] = {"sections": list(rules_approved.values())}

    files["catalogue_dev_added.json"] = {"sections": [
        _section("skill_ontology", skills["keys"], skills["fields"], approved=bool(reviewed),
                 note=(f"Reviewed {reviewed} (docs/seed_review_dev_added_skills.md): skills added in dev by hand "
                       "or by JD calibration. " if reviewed else
                       "PENDING REVIEW (production checklist): skills added in dev by hand or by JD "
                       "calibration. Review each - drop client-specific or personal terms - then set approved. ")
                      + f"Not seeded (merged or dropped in dev): {retired}.",
                 add=dev_added, change=dev_changed, renames=pending_renames),
        *rules_pending.values()]}
    if reviewed:
        for section in files["catalogue_dev_added.json"]["sections"]:
            section["approved"] = True
            if section["table"] != "skill_ontology":
                section["note"] = f"Reviewed {reviewed}: needs the dev-added skill it names (same file)."

    settings = diff["platform_config"]
    approved_settings, dropped, excluded = {}, [], []
    for key, fields in settings["change"].items():
        name = json.loads(key)[0]
        value = fields.get("value")
        if name in EXCLUDED_SETTINGS:
            excluded.append(name)
        elif value and _same_number(value["from"], value["to"]) and set(fields) == {"value"}:
            dropped.append(name)
        else:
            approved_settings[key] = fields
    files["platform.json"] = {"sections": [
        _section("platform_config", settings["keys"], settings["fields"], approved=True,
                 note=f"Scoring v2 and model-capability config and the default weights built in dev. Format-only "
                      f"edits dropped: {sorted(dropped)}. Never seeded: {sorted(excluded)} - set in production after "
                      f"enabling billing.",
                 add=settings["add"], change=approved_settings),
        _section("prompt_templates", diff["prompt_templates"]["keys"], diff["prompt_templates"]["fields"],
                 approved=True, note="Prompt template edits made in dev.",
                 add=diff["prompt_templates"]["add"], change=diff["prompt_templates"]["change"]),
        _section("embedding_model_versions", diff["embedding_model_versions"]["keys"],
                 diff["embedding_model_versions"]["fields"], approved=True,
                 note="The active embedding model - a fresh database has none.",
                 add=diff["embedding_model_versions"]["add"]),
    ]}
    files["catalogue.json"] = {"sections": [files["catalogue.json"]]}
    return files


if __name__ == "__main__":
    args = sys.argv[1:]
    reviewed_on = None
    if "--reviewed" in args:
        reviewed_on = args[args.index("--reviewed") + 1]
        del args[args.index("--reviewed"):args.index("--reviewed") + 2]
    diff_data = json.load(open(args[0], encoding="utf-8"))
    out_dir = args[1] if len(args) > 1 else os.path.join("app", "seeds", "product_config")
    os.makedirs(out_dir, exist_ok=True)
    for name, content in build(diff_data, reviewed=reviewed_on).items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=1)
            f.write("\n")
        counts = [(s["table"], len(s["add"]), len(s["change"]), s["approved"]) for s in content["sections"]]
        print(name, counts)
