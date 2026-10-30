"""
The golden set: tests/golden/golden_set.json, a self-contained, versioned record of the synthetic
candidates scoring is checked against. Everything needed to score a case without dev's database or
the parser is in the repo:

  tests/golden/golden_set.json      the cases and their expectations (this module validates it)
  tests/golden/jds/<key>.json       a JD: its row, calibration, JD skills, unknown skills, campaign settings
  tests/golden/resumes/<id>.json    a frozen parse: redacted text, parsed_json, matched skills, resume row

A case:
  {"id": "F1", "set": "phase3", "jd": "JD-F", "resume": "resumes/F1.json",
   "source_docx": "tests/fixtures/phase3/F1.docx",
   "expected": {"outcomes": ["PASS"], "core": "4/4", "note": "..."},
   "counterfactual": {"group": "NAME-F1", "varies": "name", "compare_with": "F1", "detail": "..."}}
                                                              (counterfactual cases only)

expected.outcomes: the outcomes accepted (PASS / HOLD / REJECT). expected.core: "n/m" when the
expectation is exact, else null (the designed text is kept in expected.note). A counterfactual case differs from the case
named in compare_with only in what it varies (name, career gap or region); the two must have the
same outcome and the same core-skill results. A "control" is its group's base unchanged, parsed fresh with
the variants: the variants are compared with it, and it with the original case (that difference is parse
drift, not fairness).

Add a case: put the resume .docx under tests/fixtures/, add the case here, then freeze its parse with
`python -m scripts.golden.refreeze --only <id>` (shows the parse, writes it with --write).
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "golden"
GOLDEN_SET = GOLDEN_DIR / "golden_set.json"
SCHEMA_VERSION = 1
OUTCOMES = {"PASS", "HOLD", "REJECT"}
SETS = {"phase3", "hard_negative", "senior", "counterfactual"}
VARIES = {"name", "career_gap", "region", "control"}
_CORE = re.compile(r"^\d+/\d+$")


class GoldenSetError(ValueError):
    pass


def load(path: Path = GOLDEN_SET) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate(data, Path(path).parent)
    if problems:
        raise GoldenSetError("; ".join(problems))
    return data


def load_json(relative: str, base: Path = GOLDEN_DIR) -> dict:
    return json.loads((base / relative).read_text(encoding="utf-8"))


def validate(data: dict, base: Path = GOLDEN_DIR) -> list[str]:
    """Every problem with the golden set's structure; empty when it is valid."""
    problems = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    jds = data.get("jds") or {}
    for key, relative in jds.items():
        if not (base / relative).exists():
            problems.append(f"JD {key}: {relative} is missing")
    seen, counterfactuals = set(), {}
    for case in data.get("cases") or []:
        cid = case.get("id")
        where = f"case {cid}"
        if not cid or cid in seen:
            problems.append(f"{where}: missing or duplicate id")
        seen.add(cid)
        if case.get("set") not in SETS:
            problems.append(f"{where}: set must be one of {sorted(SETS)}")
        if case.get("jd") not in jds:
            problems.append(f"{where}: unknown JD {case.get('jd')!r}")
        if not case.get("resume"):
            problems.append(f"{where}: no frozen resume")
        elif not (base / case["resume"]).exists():
            problems.append(f"{where}: {case['resume']} is missing - freeze it (scripts.golden.refreeze)")
        if not case.get("source_docx") or not (base.parents[1] / case["source_docx"]).exists():
            problems.append(f"{where}: source_docx {case.get('source_docx')!r} is missing")
        expected = case.get("expected") or {}
        outcomes = expected.get("outcomes") or []
        if not outcomes or set(outcomes) - OUTCOMES:
            problems.append(f"{where}: expected.outcomes must be a non-empty subset of {sorted(OUTCOMES)}")
        core = expected.get("core")
        if core is not None and not _CORE.match(str(core)):
            problems.append(f"{where}: expected.core must be 'n/m' or null")
        counterfactual = case.get("counterfactual")
        if (case.get("set") == "counterfactual") != bool(counterfactual):
            problems.append(f"{where}: counterfactual cases (and only they) need a counterfactual block")
        if counterfactual:
            if counterfactual.get("varies") not in VARIES:
                problems.append(f"{where}: counterfactual.varies must be one of {sorted(VARIES)}")
            if not counterfactual.get("group"):
                problems.append(f"{where}: counterfactual.group is required")
            counterfactuals[cid] = case
    by_id = {c.get("id"): c for c in data.get("cases") or []}
    for cid, case in counterfactuals.items():
        partner = by_id.get(case["counterfactual"].get("compare_with"))
        if partner is None or partner is case:
            problems.append(f"case {cid}: counterfactual.compare_with must name another case")
        elif partner.get("jd") != case.get("jd"):
            problems.append(f"case {cid}: compared with {partner['id']}, which is scored against another JD")
    return problems


def counterfactual_pairs(data: dict) -> list[tuple[dict, dict]]:
    """(case, the case it must match) for every counterfactual case."""
    by_id = {c["id"]: c for c in data.get("cases") or []}
    return [(c, by_id[c["counterfactual"]["compare_with"]]) for c in data.get("cases") or []
            if c.get("counterfactual")]
