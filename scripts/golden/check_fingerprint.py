"""
The golden-set merge check, runnable anywhere (no database, no model): the accepted golden run was
accepted at the current scoring fingerprint and covers every golden case. Exit 1 otherwise.

    python -m scripts.golden.check_fingerprint

A blocking gate in scripts/quality_check.sh. On failure: run the golden set, review the report, and
accept it in the same change (python -m scripts.golden.run, then --accept-run <report.json>).
"""
import json
import sys

from scripts.golden.fingerprint import scoring_fingerprint
from scripts.golden.golden_format import GOLDEN_DIR, load

ACCEPTED = GOLDEN_DIR / "accepted_run.json"
# The no-LLM fallback's accepted run (run.py --no-llm): held to the same fingerprint.
ACCEPTED_NO_LLM = GOLDEN_DIR / "accepted_run_no_llm.json"


def check() -> list[str]:
    problems = []
    current = scoring_fingerprint()
    cases = {c["id"] for c in load()["cases"]}
    for path, label, how in ((ACCEPTED, "golden run", ""), (ACCEPTED_NO_LLM, "no-LLM golden run", " --no-llm")):
        if not path.exists():
            problems.append(f"no accepted {label} (tests/golden/{path.name}) - run python -m scripts.golden.run{how}")
            continue
        accepted = json.loads(path.read_text(encoding="utf-8"))
        if accepted.get("fingerprint") != current:
            problems.append(f"scoring changed since the {label} was accepted on {accepted.get('accepted_on')} "
                            f"(fingerprint {accepted.get('fingerprint', '')[:12]} accepted, {current[:12]} now)")
        missing = cases - set(accepted.get("cases") or {})
        if missing:
            problems.append(f"the accepted {label} doesn't cover: {sorted(missing)}")
    return problems


if __name__ == "__main__":
    found = check()
    for problem in found:
        print("GOLDEN SET:", problem)
    if found:
        print("Run `python -m scripts.golden.run` (and `--no-llm`), review tests/golden/runs/<stamp>.md, then "
              "`python -m scripts.golden.run --accept-run tests/golden/runs/<stamp>.json` for each, in this change.")
    else:
        print("Golden set: the accepted runs (full and no-LLM) match the current scoring fingerprint.")
    sys.exit(1 if found else 0)
