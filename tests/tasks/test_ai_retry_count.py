"""
The AI evaluation reads MAX_AI_RETRY_COUNT (seeded 3) for its transient-failure retries instead of a
hardcoded 3 (2026-11-11; item 30 did the same for MAX_EMAIL_RETRY_COUNT).
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.safe_errors import SafeRetryError

MODULE = "app.tasks.ai_evaluation_tasks"


def _config(raw):
    config = MagicMock()
    config.get_configs_by_keys.return_value = {} if raw is None else {"MAX_AI_RETRY_COUNT": raw}
    return config


@pytest.mark.parametrize("raw, attempts", [
    ("3", 3), ("5", 5), (None, 3),          # the seed, a change, no row
    ("0", 3), ("50", 3), ("lots", 3),       # out of range or not a number: the default
])
def test_the_policy_reads_the_setting(raw, attempts):
    from app.tasks.ai_evaluation_tasks import _ai_evaluation_retry_policy
    with patch(f"{MODULE}.ConfigRepository", return_value=_config(raw)):
        assert _ai_evaluation_retry_policy(MagicMock()).max_attempts == attempts


def test_an_unreadable_setting_falls_back_to_the_default():
    from app.tasks.ai_evaluation_tasks import _ai_evaluation_retry_policy
    with patch(f"{MODULE}.ConfigRepository", side_effect=RuntimeError("db down")):
        assert _ai_evaluation_retry_policy(MagicMock()).max_attempts == 3


@pytest.mark.parametrize("retries_so_far, retried", [(3, True), (4, False)])
def test_a_transient_failure_retries_up_to_the_configured_count(retries_so_far, retried):
    """MAX_AI_RETRY_COUNT=5: attempt 4 still retries (the hardcoded 3 would have dead-lettered it); attempt 5
    is the last and dead-letters."""
    from app.tasks.ai_evaluation_tasks import calculate_ai_evaluation_task
    dead_letters = MagicMock()
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.ConfigRepository", return_value=_config("5")), \
            patch(f"{MODULE}.CampaignCandidateRepository", side_effect=TimeoutError("provider timed out")), \
            patch(f"{MODULE}.DeadLetterQueueRepository", return_value=dead_letters):
        calculate_ai_evaluation_task.push_request(retries=retries_so_far, id=str(uuid4()))
        try:
            if retried:
                with pytest.raises(SafeRetryError):
                    calculate_ai_evaluation_task(str(uuid4()))
                dead_letters.create.assert_not_called()
            else:
                calculate_ai_evaluation_task(str(uuid4()))
                dead_letters.create.assert_called_once()
                assert dead_letters.create.call_args.kwargs["retry_count"] == 5
        finally:
            calculate_ai_evaluation_task.pop_request()
