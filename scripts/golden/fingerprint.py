"""
The scoring fingerprint: a hash of every committed file that can change a golden-set result - the
scoring and detection code, the confirmation prompt (it lives in that code), the experience and
education validation, the JD calibration rules, the production seed data, and the golden set itself
(its cases, JDs and frozen resumes - not its expectations and notes, which change no score).

The accepted golden run (tests/golden/accepted_run.json) records the fingerprint it was accepted at.
tests/golden/test_accepted_run.py fails on every merge where they differ: run the golden set and
accept it first (python -m scripts.golden.run, then --accept).

Not covered: settings and model assignments held only in a database. The accepted run records the
model that answered, and the vote cache is keyed by it.
"""
import hashlib
import json
from pathlib import Path

from scripts.golden.golden_format import ROOT

SCORING_PATHS = (
    "app/services/skills/*.py",
    "app/services/campaign/scoring_v2.py",
    "app/services/campaign/scoring_v2_service.py",
    "app/services/campaign/near_miss.py",
    "app/services/campaign/scoring_versions.py",
    "app/services/campaign/validation_inputs.py",
    "app/services/campaign/score_display.py",
    "app/services/campaign/experience_education_validation_service.py",
    "app/services/campaign/domain_capability_matching_service.py",
    "app/services/education/*.py",
    "app/services/jd/jd_calibration_rules.py",
    "app/services/jd/jd_profile_templates.py",
    "app/services/jd/jd_role_domain.py",
    "app/services/jd/jd_skill_importance.py",
    "app/services/resume/work_experience_duration.py",
    "app/tasks/deterministic_scoring_tasks.py",
    "app/seeds/seed_scoring_v2.py",
    "app/seeds/product_config/*.json",
    "tests/golden/golden_set.json",
    "tests/golden/jds/*.json",
    "tests/golden/resumes/*.json",
)


def scoring_files(root: Path = ROOT) -> list[Path]:
    files = set()
    for pattern in SCORING_PATHS:
        files |= {p for p in root.glob(pattern) if p.is_file()}
    return sorted(files)


def scoring_fingerprint(root: Path = ROOT) -> str:
    """sha256 over each file's path and content, line endings normalised (Windows and CI agree)."""
    digest = hashlib.sha256()
    for path in scoring_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        if relative == "tests/golden/golden_set.json":
            content = _without_expectations(content)
        digest.update(content + b"\0")
    return digest.hexdigest()


# Expectations and notes don't change a score: editing them needs no new golden run.
_EXPECTATION_FIELDS = ("expected", "expected_no_llm", "tests", "trap")


def _without_expectations(content: bytes) -> bytes:
    data = json.loads(content)
    data.pop("description", None)
    for case in data.get("cases") or []:
        for field in _EXPECTATION_FIELDS:
            case.pop(field, None)
        (case.get("counterfactual") or {}).pop("detail", None)
    return json.dumps(data, sort_keys=True).encode("utf-8")
