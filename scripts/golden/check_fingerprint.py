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


def check() -> list[str]:
    if not ACCEPTED.exists():
        return ["no accepted golden run (tests/golden/accepted_run.json)"]
    accepted = json.loads(ACCEPTED.read_text(encoding="utf-8"))
    problems = []
    current = scoring_fingerprint()
    if accepted.get("fingerprint") != current:
        problems.append(f"scoring changed since the golden run was accepted on {accepted.get('accepted_on')} "
                        f"(fingerprint {accepted.get('fingerprint', '')[:12]} accepted, {current[:12]} now)")
    missing = {c["id"] for c in load()["cases"]} - set(accepted.get("cases") or {})
    if missing:
        problems.append(f"the accepted run doesn't cover: {sorted(missing)}")
    return problems


if __name__ == "__main__":
    found = check()
    for problem in found:
        print("GOLDEN SET:", problem)
    if found:
        print("Run `python -m scripts.golden.run`, review tests/golden/runs/<stamp>.md, then "
              "`python -m scripts.golden.run --accept-run tests/golden/runs/<stamp>.json` in this change.")
    else:
        print("Golden set: the accepted run matches the current scoring fingerprint.")
    sys.exit(1 if found else 0)
