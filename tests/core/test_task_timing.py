"""
Review item 20: no task can be held by a worker longer than the broker's visibility timeout, or Redis
hands it to a second worker and it runs twice. Held = the longest retry countdown + the longest run.
"""
import ast
from datetime import timedelta
from pathlib import Path

import app
from app.core import celery_app as module


def test_a_held_message_never_outlives_the_visibility_timeout():
    assert module.MAX_RETRY_COUNTDOWN_SECONDS + module.TASK_TIME_LIMIT_SECONDS < module.VISIBILITY_TIMEOUT_SECONDS
    assert module.TASK_SOFT_TIME_LIMIT_SECONDS < module.TASK_TIME_LIMIT_SECONDS
    conf = module.celery_app.conf
    assert conf.broker_transport_options["visibility_timeout"] == module.VISIBILITY_TIMEOUT_SECONDS
    assert conf.task_time_limit == module.TASK_TIME_LIMIT_SECONDS
    assert conf.task_soft_time_limit == module.TASK_SOFT_TIME_LIMIT_SECONDS


def test_countdowns_are_capped():
    assert module.bounded_countdown(10) == 10
    assert module.bounded_countdown(10_000) == module.MAX_RETRY_COUNTDOWN_SECONDS
    assert module.bounded_countdown(-5) == 0


def test_every_retry_policy_fits_under_the_cap():
    from app.services.document_processing.retry_policy import compute_backoff_seconds
    from app.tasks.deterministic_scoring_tasks import _DECISION_PENDING_RETRY_POLICY
    from app.tasks.scoring_v2_tasks import _SCORING_V2_RETRY_POLICY
    for policy in (_DECISION_PENDING_RETRY_POLICY, _SCORING_V2_RETRY_POLICY):
        longest = max(compute_backoff_seconds(policy, n) for n in range(1, policy.max_attempts))
        assert longest <= module.MAX_RETRY_COUNTDOWN_SECONDS


def test_the_recovery_age_limits_wait_for_the_broker():
    """A sweep acting before the broker's own redelivery would run the task twice."""
    from app.tasks.resume_processing_tasks import STALLED_IN_FLIGHT_MINUTES
    from app.core.task_dispatch import IN_FLIGHT_STALE_AFTER
    visibility = timedelta(seconds=module.VISIBILITY_TIMEOUT_SECONDS)
    assert IN_FLIGHT_STALE_AFTER > visibility
    assert timedelta(minutes=STALLED_IN_FLIGHT_MINUTES) > visibility


def test_every_retry_countdown_goes_through_bounded_countdown():
    root = Path(app.__file__).parent
    unbounded = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("retry", "apply_async"):
                for keyword in node.keywords:
                    if keyword.arg != "countdown":
                        continue
                    value = keyword.value
                    bounded = (isinstance(value, ast.Call) and getattr(value.func, "id", None) == "bounded_countdown") \
                        or (isinstance(value, ast.Constant) and value.value <= module.MAX_RETRY_COUNTDOWN_SECONDS)
                    if not bounded:
                        unbounded.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not unbounded, f"countdowns not capped by bounded_countdown: {unbounded}"
