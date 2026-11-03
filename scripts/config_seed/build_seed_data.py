"""
Turn a reviewed diff (diff_config.py: dev vs a fresh database) into the seed
data files app/seeds/seed_product_config.py loads:

    python scripts/config_seed/build_seed_data.py <diff.json> [out dir] [--reviewed YYYY-MM-DD [--approve-pending]]

--reviewed: the dev-added skills (and the rules naming them) were reviewed on that
date - their sections are written approved, with the date in the note. Without
it they are written unapproved; a regeneration never approves anything by itself.
Skills added in dev after the committed file was written stay pending either way.
--approve-pending: the skills in the committed file's pending section were reviewed
too - they're approved with the rest. Skills not in the committed file at all still
wait for review.

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
# SCORING_V2_CONFIG: no longer read (retire v1, phase E) - the scoring seed creates the current scoring
# version with today's configuration instead.
EXCLUDED_SETTINGS = {"SCORING_V2_DAILY_LLM_CALL_CAP", "SCORING_V2_CONFIG"}
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


def reviewed_skill_names(path, include_pending: bool = False) -> set[str] | None:
    """
    Lower-cased names of the dev-added skills an earlier review approved (the committed file), or None.
    include_pending: the skills in its pending section count as reviewed too (--approve-pending).
    """
    if not os.path.exists(path):
        return None
    names = set()
    for section in json.load(open(path, encoding="utf-8"))["sections"]:
        if section["table"] == "skill_ontology" and (section["approved"] or include_pending):
            names |= {json.loads(k)[0].lower() for k in list(section["add"]) + list(section["change"])}
    return names


def build(diff: dict, reviewed: str | None = None, reviewed_names: set[str] | None = None) -> dict:
    files = {}
    skills = diff["skill_ontology"]
    dev_added_all = {k: v for k, v in skills["add"].items() if v.get("source") in DEV_ADDED_SOURCES}
    # merged or dropped in dev (deactivated): nothing to seed into a fresh database
    retired = sorted(json.loads(k)[0] for k, v in dev_added_all.items() if v.get("is_active") is False)
    dev_added = {k: v for k, v in dev_added_all.items() if v.get("is_active") is not False}
    base_add = {k: v for k, v in skills["add"].items() if k not in dev_added_all}
    dev_changed = {k: v for k, v in skills["change"].items() if (v.get("source") or {}).get("to") in DEV_ADDED_SOURCES}
    base_change = {k: v for k, v in skills["change"].items() if k not in dev_changed}
    # Skills added in dev after the last review stay unapproved on their own, whatever --reviewed says.
    new_added, new_changed = {}, {}
    if reviewed and reviewed_names is not None:
        unreviewed = lambda key: json.loads(key)[0].lower() not in reviewed_names
        new_added = {k: v for k, v in dev_added.items() if unreviewed(k)}
        new_changed = {k: v for k, v in dev_changed.items() if unreviewed(k)}
        dev_added = {k: v for k, v in dev_added.items() if k not in new_added}
        dev_changed = {k: v for k, v in dev_changed.items() if k not in new_changed}
    new_skill_names = {json.loads(k)[0] for k in list(new_added) + list(new_changed)}
    pending_skill_names = {json.loads(k)[0] for k in list(dev_added) + list(dev_changed)} | new_skill_names
    # a rename travels with the row it renames to
    renames = RENAMES["skill_ontology"]
    pending_renames = {old: new for old, new in renames.items() if new in pending_skill_names}
    base_renames = {old: new for old, new in renames.items() if old not in pending_renames}

    files["catalogue.json"] = _section(
        "skill_ontology", skills["keys"], skills["fields"], approved=True,
        note="The skill catalogue: bulk-imported and seeded skills and corrections to the stop-gap seed "
             "rows. Embeddings are not seeded - run scripts/generate_missing_skill_embeddings.py after loading.",
        add=base_add, change=base_change, renames=base_renames)

    rules_approved, rules_pending, rules_new = {}, {}, {}
    for table in ("skill_senses", "skill_pattern_rules", "certification_skills"):
        t = diff[table]
        ok = {k: v for k, v in t["add"].items() if v.get("skill") not in pending_skill_names}
        new = {k: v for k, v in t["add"].items() if v.get("skill") in new_skill_names}
        held = {k: v for k, v in t["add"].items() if k not in ok and k not in new}
        rules_approved[table] = _section(table, t["keys"], t["fields"], approved=True,
                                         note="Needs the catalogue loaded first.", add=ok)
        if held:
            rules_pending[table] = _section(table, t["keys"], t["fields"], approved=False,
                                            note="Waits for the dev-added skill it names.", add=held)
        if new:
            rules_new[table] = _section(table, t["keys"], t["fields"], approved=False,
                                        note=f"PENDING REVIEW: names a skill added after the {reviewed} review.", add=new)
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
    if new_added or new_changed or rules_new:
        files["catalogue_dev_added.json"]["sections"] += [
            _section("skill_ontology", skills["keys"], skills["fields"], approved=False,
                     note=f"PENDING REVIEW: skills added in dev after the {reviewed} review "
                          "(docs/seed_review_dev_added_skills.md, 'Added after the review'). Set approved once reviewed.",
                     add=new_added, change=new_changed),
            *rules_new.values()]

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
    approve_pending = "--approve-pending" in args
    if approve_pending:
        args.remove("--approve-pending")
        if not reviewed_on:
            sys.exit("--approve-pending needs --reviewed YYYY-MM-DD")
    diff_data = json.load(open(args[0], encoding="utf-8"))
    out_dir = args[1] if len(args) > 1 else os.path.join("app", "seeds", "product_config")
    os.makedirs(out_dir, exist_ok=True)
    # what the last review approved - read before this run overwrites the file
    already_reviewed = (reviewed_skill_names(os.path.join(out_dir, "catalogue_dev_added.json"),
                                             include_pending=approve_pending) if reviewed_on else None)
    for name, content in build(diff_data, reviewed=reviewed_on, reviewed_names=already_reviewed).items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=1)
            f.write("\n")
        counts = [(s["table"], len(s["add"]), len(s["change"]), s["approved"]) for s in content["sections"]]
        print(name, counts)
