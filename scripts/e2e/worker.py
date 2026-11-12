"""
Celery worker for the isolated e2e stack: the normal worker, with the stub LLM
installed unless E2E_LLM=real (then the configured provider / GEMINI_API_KEY
from .env.e2e is used). Refuses to start unless the stack is isolated.

    python -m scripts.e2e.worker
"""
import os

from scripts.e2e.setup_e2e import assert_isolated


def main() -> None:
    assert_isolated()
    from app.core.celery_app import celery_app
    # Load every task module first, so the stub also replaces names they imported.
    celery_app.loader.import_default_modules()   # conf.imports + conf.include
    if os.environ.get("E2E_LLM", "stub").lower() != "real":
        from scripts.e2e.stub_llm import install
        install()
        print("e2e worker: stub LLM installed (no network LLM calls)")
    _e2e_scoring_switches()
    _drop_working_directory_from_path()
    celery_app.worker_main(["worker", "--loglevel=info", "--pool=solo", "--concurrency=1"])


def _e2e_scoring_switches() -> None:
    """
    e2e only - never in a real worker:
    - the deciding path's retry backoff in seconds, not minutes (same 4 attempts);
    - e2e_stub_mode.json {"last_attempt_fallback": false} turns off the no-LLM fallback on the last
      attempt, so a provider outage runs on to the dead-letter queue (phase A's path, which the
      fallback otherwise replaces) and its replay can be checked.
    """
    from app.services.document_processing.retry_policy import RetryPolicy
    from app.services.campaign.scoring_v2_service import FALLBACK_ON_CAP, FALLBACK_ON_UNAVAILABLE
    from app.tasks import deterministic_scoring_tasks as tasks
    from scripts.e2e.stub_llm import mode

    tasks._DECISION_PENDING_RETRY_POLICY = RetryPolicy(max_attempts=4, base_delay_seconds=5, max_delay_seconds=20)
    real = tasks._run_scoring_v2

    def run_scoring_v2(*args, llm_fallback=None, **kwargs):
        if llm_fallback == FALLBACK_ON_UNAVAILABLE and mode().get("last_attempt_fallback") is False:
            llm_fallback = FALLBACK_ON_CAP
        return real(*args, llm_fallback=llm_fallback, **kwargs)

    tasks._run_scoring_v2 = run_scoring_v2
    print("e2e worker: retry backoff in seconds; last-attempt fallback switchable (e2e_stub_mode.json)")


def _drop_working_directory_from_path() -> None:
    """
    The celery command puts the working directory on sys.path only while it loads the app, then takes it off
    again (celery.utils.imports.cwd_in_path); `python -m` here would leave it on for good. Taken off, so a task
    that imports something outside app/ (the golden runner) runs as it does in a real worker - it fails here
    unless it puts the project root on the path itself (app/services/settings/golden_runs.golden_runner).
    The scripts modules this worker loaded itself (scripts.e2e.*) are unloaded as well: a real worker never
    imports scripts, and Python keeps a namespace package's search path once found, so with them loaded
    scripts.golden would still resolve. The stub keeps working - its functions are already installed.
    """
    import sys
    root = os.path.realpath(os.getcwd())
    sys.path[:] = [p for p in sys.path if p not in ("", ".") and os.path.realpath(p) != root]
    for name in [m for m in sys.modules if m == "scripts" or m.startswith("scripts.")]:
        del sys.modules[name]
    print("e2e worker: working directory off sys.path and scripts unloaded, as under the celery command")


if __name__ == "__main__":
    main()
