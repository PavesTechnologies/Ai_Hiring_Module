"""
The required check before merging: the accepted golden run was accepted at the current scoring
fingerprint. A change to the scoring or detection code, the prompt, the seed data or the golden set
changes the fingerprint - run the golden set and accept it in the same change:

    python -m scripts.golden.run            (review the report under tests/golden/runs/)
    python -m scripts.golden.run --accept
"""
import json

from scripts.golden.fingerprint import scoring_files, scoring_fingerprint
from scripts.golden.golden_format import GOLDEN_DIR, load

ACCEPTED = GOLDEN_DIR / "accepted_run.json"


def test_the_accepted_golden_run_matches_the_current_scoring_fingerprint():
    assert ACCEPTED.exists(), "no accepted golden run - run python -m scripts.golden.run --accept"
    accepted = json.loads(ACCEPTED.read_text(encoding="utf-8"))
    assert accepted["fingerprint"] == scoring_fingerprint(), (
        "Scoring changed since the golden run was accepted (code, prompt, seed data or golden set). Run "
        "`python -m scripts.golden.run`, review the report, then accept it with --accept in this change.")


def test_the_accepted_run_covers_every_golden_case():
    accepted = json.loads(ACCEPTED.read_text(encoding="utf-8"))
    assert set(accepted["cases"]) == {c["id"] for c in load()["cases"]}


def test_the_fingerprint_covers_the_scoring_code_the_prompt_and_the_seed():
    files = {p.relative_to(GOLDEN_DIR.parents[1]).as_posix() for p in scoring_files()}
    for expected in ("app/services/skills/core_skill_confirmation_service.py",     # the confirmation prompt
                     "app/services/campaign/scoring_v2_service.py",
                     "app/services/skills/resume_skill_scanner.py",
                     "app/seeds/product_config/catalogue.json",
                     "tests/golden/golden_set.json"):
        assert expected in files


def test_the_fingerprint_ignores_line_endings(tmp_path):
    (tmp_path / "app" / "services" / "skills").mkdir(parents=True)
    target = tmp_path / "app" / "services" / "skills" / "x.py"
    target.write_bytes(b"a = 1\nb = 2\n")
    unix = scoring_fingerprint(tmp_path)
    target.write_bytes(b"a = 1\r\nb = 2\r\n")
    assert scoring_fingerprint(tmp_path) == unix
    target.write_bytes(b"a = 1\nb = 3\n")
    assert scoring_fingerprint(tmp_path) != unix


def test_the_ci_gate_passes_now_and_names_a_stale_fingerprint(monkeypatch):
    from scripts.golden import check_fingerprint
    assert check_fingerprint.check() == []
    monkeypatch.setattr(check_fingerprint, "scoring_fingerprint", lambda: "f" * 64)
    assert any("scoring changed" in p for p in check_fingerprint.check())
