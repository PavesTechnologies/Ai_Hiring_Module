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
    celery_app.worker_main(["worker", "--loglevel=info", "--pool=solo", "--concurrency=1"])


if __name__ == "__main__":
    main()
