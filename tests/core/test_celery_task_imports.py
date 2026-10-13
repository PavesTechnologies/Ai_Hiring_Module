"""
The worker only registers tasks from modules in celery_app.conf.imports (or
pulled in by them). A task module left off the list is silently unknown to
the worker - its messages are dropped ("unregistered task"). Every module in
app/tasks/ must be listed.
"""
from pathlib import Path

import app.tasks
from app.core.celery_app import celery_app

TASKS_DIR = Path(app.tasks.__file__).parent


def _task_modules() -> set[str]:
    return {f"app.tasks.{p.stem}" for p in TASKS_DIR.glob("*.py") if p.stem != "__init__"}


def test_every_task_module_is_in_the_worker_imports():
    missing = _task_modules() - set(celery_app.conf.imports)
    assert not missing, f"add to celery_app.conf.imports: {sorted(missing)}"


def test_the_imports_list_names_only_real_task_modules():
    assert set(celery_app.conf.imports) <= _task_modules()


def test_the_unknown_skill_reevaluation_task_registers():
    celery_app.loader.import_default_modules()
    assert "scoring.reevaluate_candidates_for_unknown_skill" in celery_app.tasks
