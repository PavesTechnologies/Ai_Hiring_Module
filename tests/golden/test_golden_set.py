"""The golden set is valid, self-contained, and its counterfactual pairs differ only in what they vary."""
import difflib
import hashlib
import importlib.util
import json
import re
from functools import lru_cache

import pytest

from scripts.golden.golden_format import (
    GOLDEN_DIR, ROOT, counterfactual_pairs, load, load_json, validate,
)
from scripts.golden.refreeze import _list_diff, diff_frozen

GOLDEN = load()
CASES = GOLDEN["cases"]
CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{8,}\d")


@lru_cache(maxsize=None)
def frozen(relative: str) -> dict:
    return load_json(relative)


@lru_cache(maxsize=None)
def local_text(docx: str) -> str:
    """The parse's first stages, locally: extraction, cleaning, PII redaction (no LLM)."""
    from app.models.resume.resume_source_format import ResumeSourceFormat
    from app.services.ai.preprocessing_service import PreprocessingService
    from app.services.pii.pii_detection_service import PIIDetectionService
    from app.services.pii.pii_redaction_service import PIIRedactionService
    from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
    text = PreprocessingService().normalize(
        ResumeTextExtractionService.extract((ROOT / docx).read_bytes(), ResumeSourceFormat.DOCX))
    return PIIRedactionService().redact(text, PIIDetectionService().detect(text))


def _changed_lines(a: str, b: str) -> list[str]:
    return [line for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and line[:3] not in ("+++", "---")]


# --- the committed set --------------------------------------------------------------------------

def test_the_committed_golden_set_is_valid_and_complete():
    from collections import Counter
    counts = Counter(c["set"] for c in CASES)
    assert (counts["phase3"], counts["hard_negative"], counts["senior"]) == (16, 10, 8)
    assert counts["counterfactual"] >= 1


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_each_frozen_resume_is_the_redacted_text_of_its_source_docx(case):
    resume = frozen(case["resume"])
    text = resume["redacted_text"]
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == resume["text_sha256"]
    assert text == local_text(case["source_docx"]), "the .docx no longer gives the frozen text - refreeze it"
    assert not CONTACT.search(text), "contact details left in the frozen text"
    assert resume["parsed_json"] and resume["candidate_skills"] is not None


@pytest.mark.parametrize("key", sorted(GOLDEN["jds"]))
def test_each_jd_is_calibrated_with_core_skills(key):
    jd = load_json(GOLDEN["jds"][key])
    assert jd["job_description"]["calibration_status"] == "CALIBRATED"
    assert any(s.get("importance") == "CORE" for s in jd["jd_skills"])
    assert jd["campaign"]["prompt_template"]


PAIRS = counterfactual_pairs(GOLDEN)


@pytest.mark.parametrize("case, partner", PAIRS, ids=[c["id"] for c, _ in PAIRS])
def test_a_counterfactual_differs_from_its_partner_only_in_what_it_varies(case, partner):
    a, b = frozen(partner["resume"])["redacted_text"], frozen(case["resume"])["redacted_text"]
    changed = _changed_lines(a, b)
    varies = case["counterfactual"]["varies"]
    if varies == "control":
        assert changed == []                                       # the base unchanged
    elif varies == "region":
        assert len(changed) == 2 and all("[EMAIL] · [PHONE] · " in line for line in changed)
    elif varies == "career_gap" and (partner.get("counterfactual") or {}).get("varies") == "career_gap":
        # the same gap left silent and stated: only the stated break's line
        assert len(changed) == 1 and changed[0].startswith(("+", "-")) and re.search(r"\b20\d\d\b", changed[0])
    elif varies == "career_gap":
        assert 2 <= len(changed) <= 3 and all(re.search(r"\b20\d\d\b", line) for line in changed)
    else:                                                          # name: the heading, and the summary if named there
        new_name = frozen(case["resume"])["parsed_json"].get("full_name") or ""
        added = [line for line in changed if line.startswith("+")]
        assert added and all(new_name in line for line in added), changed
        assert len(added) == len(changed) - len(added)              # each changed line replaced, nothing else


# --- the validator ------------------------------------------------------------------------------

def _minimal(**case_overrides) -> dict:
    case = {"id": "X1", "set": "phase3", "jd": "JD-F", "resume": "resumes/F2.json",
            "source_docx": "tests/fixtures/phase3/F2.docx", "expected": {"outcomes": ["PASS"], "core": "4/4"}}
    case.update(case_overrides)
    return {"schema_version": 1, "jds": {"JD-F": "jds/JD-F.json"}, "cases": [case]}


def test_a_minimal_set_is_valid():
    assert validate(_minimal(), GOLDEN_DIR) == []


@pytest.mark.parametrize("override, problem", [
    ({"set": "other"}, "set must be"),
    ({"jd": "JD-X"}, "unknown JD"),
    ({"resume": "resumes/NOPE.json"}, "is missing"),
    ({"expected": {"outcomes": ["MAYBE"]}}, "expected.outcomes"),
    ({"expected": {"outcomes": ["PASS"], "core": "most"}}, "expected.core"),
    ({"set": "counterfactual"}, "need a counterfactual block"),
    ({"set": "counterfactual", "counterfactual": {"group": "G", "varies": "age", "compare_with": "X1"}},
     "varies must be"),
    ({"set": "counterfactual", "counterfactual": {"group": "G", "varies": "name", "compare_with": "X1"}},
     "compare_with must name another case"),
])
def test_the_validator_names_each_problem(override, problem):
    assert any(problem in p for p in validate(_minimal(**override), GOLDEN_DIR))


# --- the refreeze diff --------------------------------------------------------------------------

def test_the_diff_shows_field_changes_and_new_empty_fields_per_entry():
    old = [{"title": "Engineer", "company": "Acme", "start_date": "Jan 2020", "end_date": "Dec 2022"}]
    new = [{"title": "Engineer", "company": "Acme", "start_date": "Jan 2020", "end_date": "Jun 2021",
            "is_internship": False},
           {"title": "Career break", "company": None, "start_date": "Jul 2021", "end_date": "Dec 2022"}]
    lines = _list_diff(old, new)
    assert any(line.startswith("+ ") and "Career break" in line for line in lines)
    assert any("end_date" in line and "Dec 2022" in line and "Jun 2021" in line for line in lines)
    assert any("fields added, empty or false: is_internship" in line for line in lines)


def test_an_unchanged_resume_has_no_differences_and_a_new_one_says_so():
    resume = frozen(CASES[0]["resume"])
    assert diff_frozen(resume, json.loads(json.dumps(resume))) == []
    assert diff_frozen(None, resume) == ["(new - not frozen before)"]


# --- the counterfactual builder -----------------------------------------------------------------

def _builder():
    spec = importlib.util.spec_from_file_location("build_counterfactuals",
                                                  ROOT / "scripts" / "fixtures" / "build_counterfactuals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_builder_changes_only_the_varied_detail():
    builder = _builder()
    base = builder.base_lines("D1")
    renamed = builder.transform(base, ("name", "Test Person"))
    assert renamed[0].endswith("— Test Person") and "test.person@example.invalid" in "\n".join(renamed)
    assert len([1 for a, b in zip(base, renamed) if a != b]) == 2              # heading and email
    moved = builder.transform(base, ("region", "Hyderabad", "Patna"))
    assert [b for a, b in zip(base, moved) if a != b] == [base[2].replace("Hyderabad", "Patna")]
    gapped = builder.transform(base, ("gap", "ServiceNow Developer", "Jul 2020 – Jun 2022", "Jul 2020 – Apr 2021",
                                      "*Career break* · May 2021 – Jun 2022"))
    assert len(gapped) == len(base) + 2 and "*Career break* · May 2021 – Jun 2022" in gapped
    assert builder.transform(base, ("none",)) == base


def test_every_counterfactual_docx_is_built_from_the_builder():
    builder = _builder()
    built = {cid for cid, *_ in builder.VARIANTS}
    in_golden = {c["id"] for c in CASES if c["set"] == "counterfactual"}
    assert built == in_golden
    for cid in built:
        assert (ROOT / "tests" / "fixtures" / "counterfactual" / f"{cid}.docx").exists()
