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


if __name__ == "__main__":
    main()
