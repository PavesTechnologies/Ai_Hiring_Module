"""
A golden estimate started from the Settings API, run by the real worker - ISOLATED e2e stack only (e2e/README.md).

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.run_golden_estimate_e2e

The unit tests run GoldenRunService.execute with a fake runner; this drives the whole path: HR starts an estimate
for the current scoring version through POST /settings/scoring-versions/{id}/golden-runs, the task goes through
Redis to the stack's own Celery worker, which imports the golden runner (scripts.golden.run) and scores the golden
set cache-only. The e2e worker drops the working directory from sys.path before it starts, as the celery command
does after loading the app, so a runner import that only works from the project root fails here as it did in dev.

  G1  HR starts an estimate (202, QUEUED); a recruiter can't
  G2  the worker finishes it as ESTIMATED - not ERROR, not left RUNNING - with every golden case looked at
  G3  an estimate makes no LLM call (llm_call_log unchanged)
  G4  no email

Report: /data/storage/e2e_golden_estimate_report.json
"""
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.run_fallback_e2e import wait_for
from scripts.e2e.run_reparse_e2e import EmailAttempted, Report, _token_payload
from scripts.e2e.setup_e2e import assert_isolated

REPORT = Path("/data/storage/e2e_golden_estimate_report.json")
TIMEOUT = 600
FINAL = {"ESTIMATED", "PASSED", "FAILED", "STOPPED", "ERROR"}


def main() -> None:
    assert_isolated()
    import boto3
    real_client = boto3.client

    def guarded(service, *args, **kwargs):
        if service == "ses":
            raise EmailAttempted("an SES client was created")
        return real_client(service, *args, **kwargs)

    with patch("boto3.client", guarded), patch("app.middleware.jwt_middleware.decode_token", _token_payload):
        run()


def run() -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    import app.models  # noqa: F401
    from app.db.session import SessionLocal
    from app.enums.constants import API_PREFIX
    from app.main import app as api
    from app.models.llm_call_log import LLMCallLog
    from app.models.scoring_v2 import ScoringVersionGoldenRun
    from scripts.golden.golden_format import load

    report = Report()
    steps: dict = {}
    client = TestClient(api, base_url=f"http://e2e.local{API_PREFIX}", client=("127.0.0.1", 50000))
    recruiter = {"Authorization": "Bearer e2e:e2e-recruiter"}
    hr = {"Authorization": "Bearer e2e:e2e-hr-admin"}
    db = SessionLocal()
    calls_before = db.execute(select(func.count()).select_from(LLMCallLog)).scalar_one()
    cases = len(load()["cases"])

    # G1 - start
    versions = client.get("/scoring-versions", headers=hr).json().get("data") or []
    current = next((v for v in versions if v["status"] == "current"), None)
    path = f"/settings/scoring-versions/{current['id'] if current else uuid.uuid4()}/golden-runs"
    refused = client.post(path, headers=recruiter, json={"kind": "ESTIMATE"})
    started = client.post(path, headers=hr, json={"kind": "ESTIMATE"})
    run_id = (started.json().get("data") or {}).get("id")
    steps["G1"] = {"version": current and current["label"], "recruiter": refused.status_code,
                   "hr": started.status_code, "body": started.json()}
    report.check(1, "HR starts an estimate from the API (queued); a recruiter can't",
                 current is not None and refused.status_code == 403 and started.status_code == 202
                 and (started.json().get("data") or {}).get("status") == "QUEUED",
                 f"recruiter HTTP {refused.status_code}; HR HTTP {started.status_code}")

    # G2 - the real worker finishes it
    def finished():
        db.expire_all()
        row = db.get(ScoringVersionGoldenRun, uuid.UUID(run_id)) if run_id else None
        return row if row is not None and row.status in FINAL else None

    try:
        row = wait_for(finished, "the worker to finish the estimate", timeout=TIMEOUT)
    except TimeoutError:
        row = db.get(ScoringVersionGoldenRun, uuid.UUID(run_id)) if run_id else None
    estimate = ((row.summary or {}).get("estimate") or {}) if row else {}
    listed = client.get(path, headers=hr).json().get("data") or {}
    steps["G2"] = {"status": row and row.status, "stop_reason": row and row.stop_reason,
                   "expected_llm_calls": row and row.expected_llm_calls, "golden_cases": cases,
                   "estimate": {k: v for k, v in estimate.items() if not isinstance(v, (list, dict))},
                   "listed_first": (listed.get("runs") or [{}])[0].get("status")}
    report.check(2, "the worker finishes it as ESTIMATED, every golden case looked at",
                 row is not None and row.status == "ESTIMATED" and row.started_at is not None
                 and row.finished_at is not None and estimate.get("cases") == cases
                 and estimate.get("cases_scored") == cases
                 and steps["G2"]["listed_first"] == "ESTIMATED",
                 json.dumps(steps["G2"], default=str)[:400])

    # G3 - no LLM call
    calls_after = db.execute(select(func.count()).select_from(LLMCallLog)).scalar_one()
    steps["G3"] = {"llm_calls_before": calls_before, "llm_calls_after": calls_after}
    report.check(3, "an estimate makes no LLM call", calls_after == calls_before,
                 f"llm_call_log {calls_before} -> {calls_after}")

    report.check(4, "no email: no SES client created in this process", True,
                 "the worker's own count is checked by e2e.ps1")
    REPORT.write_text(json.dumps({"checks": report.rows, "steps": steps}, indent=1, default=str), "utf-8")
    print(f"{sum(r['result'] == 'PASS' for r in report.rows)}/{len(report.rows)} golden-estimate checks passed. "
          f"Report: {REPORT}")
    db.close()
    if any(r["result"] == "FAIL" for r in report.rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
