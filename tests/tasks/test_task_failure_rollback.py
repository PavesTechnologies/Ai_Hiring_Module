"""
Review item 22: a task's failure handler rolls the session back before its bookkeeping. After a
database error the session is unusable, so marking the task failed raised, hid the real error and left
the row RUNNING (and a bulk job PROCESSING).
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app

TASKS_DIR = Path(app.__file__).parent / "tasks"
WRITES = ("mark_", "commit", "update_status", "create", "update", "increment", "set_", "handle_failure")
HANDLED_TYPES = ("Exception", "BaseException", "StageExecutionError", "CampaignException")


def _call_names(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name:
                yield name


def _writes_before_rollback(handler) -> bool:
    for stmt in handler.body:
        names = list(_call_names(stmt))
        if any("rollback" in n for n in names):
            return False
        if any(n.startswith(WRITES) for n in names):
            return True
    return False


def test_every_task_failure_handler_rolls_back_before_writing():
    offenders, checked = [], 0
    for path in sorted(TASKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if not any(getattr(d.func if isinstance(d, ast.Call) else d, "attr", None) == "task"
                       for d in fn.decorator_list):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    type_name = None if handler.type is None else getattr(handler.type, "id", None)
                    if handler.type is not None and type_name not in HANDLED_TYPES:
                        continue
                    checked += 1
                    if _writes_before_rollback(handler):
                        offenders.append(f"{path.name}:{handler.lineno} ({fn.name})")
    assert checked > 30, f"only {checked} handlers found - the scan is broken"
    assert not offenders, "failure handlers that write before rolling back:\n" + "\n".join(offenders)


def test_rollback_quietly_never_raises():
    from app.db.session import rollback_quietly
    broken, fine = MagicMock(), MagicMock()
    broken.rollback.side_effect = RuntimeError("connection gone")
    rollback_quietly(broken, fine)
    broken.rollback.assert_called_once()
    fine.rollback.assert_called_once()


def test_a_scheduled_task_marks_itself_failed_after_a_database_error():
    """auto_close_expired_campaigns: the session is rolled back, then the task log is marked failed."""
    from app.tasks import campaign_tasks as mod
    db = MagicMock()
    order = []
    db.rollback.side_effect = lambda: order.append("rollback")
    log_service = MagicMock()
    log_service.create_log.return_value = MagicMock()
    log_service.mark_failure.side_effect = lambda *a, **k: order.append("mark_failure")
    scheduler = MagicMock()
    scheduler.auto_close_expired_campaigns.side_effect = RuntimeError("database error")
    with patch.object(mod, "SessionLocal", return_value=db), \
            patch.object(mod, "CeleryTaskLogService", return_value=log_service), \
            patch.object(mod, "CampaignSchedulerService", return_value=scheduler), \
            patch.object(mod, "CampaignRepository"), patch.object(mod, "AuditRepository"), \
            patch.object(mod, "CeleryTaskLogRepository"), patch.object(mod, "ConfigRepository"):
        with pytest.raises(RuntimeError):
            mod.auto_close_expired_campaigns()
    assert order == ["rollback", "mark_failure"]
