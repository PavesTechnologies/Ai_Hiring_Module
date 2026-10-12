"""
Runs every scheduled (beat) job once in the ISOLATED e2e stack and fails if any
of them tries to create an SES client. Prints only job names and outcomes.

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.check_scheduled_jobs
"""
from unittest.mock import patch

from scripts.e2e.run_reparse_e2e import EmailAttempted, run_scheduled_jobs
from scripts.e2e.setup_e2e import assert_isolated


def main() -> None:
    assert_isolated()
    import boto3
    real_client = boto3.client

    def guarded(service, *args, **kwargs):
        if service == "ses":
            raise EmailAttempted("an SES client was created")
        return real_client(service, *args, **kwargs)

    with patch("boto3.client", guarded):
        results = run_scheduled_jobs()
    for name, outcome in results.items():
        print(f"{name}: {outcome}")
    emailed = [n for n, r in results.items() if r == "EMAIL ATTEMPTED"]
    not_run = [n for n, r in results.items() if "NotRegistered" in r]
    print(f"\n{len(results)} scheduled jobs: {len(emailed)} attempted email, {len(not_run)} not found")
    if emailed or not_run:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
