from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.models.config import CBState
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository


def _repo_with_mock_db():
    db = MagicMock()
    return db, CircuitBreakerRepository(db)


def test_get_or_create_creates_closed_row_when_none_exists():
    db, repo = _repo_with_mock_db()
    db.query.return_value.filter.return_value.first.return_value = None

    state = repo.get_or_create("EMBEDDING_SERVICE", failure_threshold=5)

    assert state.state == CBState.CLOSED
    assert state.failure_count == 0
    assert state.failure_threshold == 5
    db.add.assert_called_once()


def test_get_or_create_returns_existing_row_unchanged():
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.OPEN)
    db.query.return_value.filter.return_value.first.return_value = existing

    state = repo.get_or_create("EMBEDDING_SERVICE")

    assert state is existing
    db.add.assert_not_called()


def test_increment_failure_opens_circuit_at_threshold():
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.CLOSED, failure_count=9, failure_threshold=10)
    db.query.return_value.filter.return_value.first.return_value = existing

    state, just_opened = repo.increment_failure("EMBEDDING_SERVICE")

    assert state.failure_count == 10
    assert state.state == CBState.OPEN
    assert state.retry_after is not None
    assert just_opened is True


def test_increment_failure_stays_closed_below_threshold():
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.CLOSED, failure_count=2, failure_threshold=10)
    db.query.return_value.filter.return_value.first.return_value = existing

    state, just_opened = repo.increment_failure("EMBEDDING_SERVICE")

    assert state.failure_count == 3
    assert just_opened is False


def test_increment_failure_reopens_a_failed_half_open_probe():
    """
    A HALF_OPEN probe that fails must re-open with a fresh retry_after -
    was_open only checks for CBState.OPEN, so HALF_OPEN (!= OPEN) still
    satisfies "not was_open", and failure_count is already >= threshold
    from the original OPEN transition, so this fires again immediately.
    """
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.HALF_OPEN, failure_count=10, failure_threshold=10, retry_after=None)
    db.query.return_value.filter.return_value.first.return_value = existing

    state, just_opened = repo.increment_failure("EMBEDDING_SERVICE")

    assert state.state == CBState.OPEN
    assert just_opened is True
    assert state.retry_after is not None


def test_transition_to_half_open_if_due_flips_when_cooldown_passed():
    db, repo = _repo_with_mock_db()
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    existing = MagicMock(state=CBState.OPEN, retry_after=past)
    db.query.return_value.filter.return_value.first.return_value = existing

    state = repo.transition_to_half_open_if_due("EMBEDDING_SERVICE")

    assert state.state == CBState.HALF_OPEN


def test_transition_to_half_open_if_due_stays_open_when_cooldown_not_passed():
    db, repo = _repo_with_mock_db()
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    existing = MagicMock(state=CBState.OPEN, retry_after=future)
    db.query.return_value.filter.return_value.first.return_value = existing

    state = repo.transition_to_half_open_if_due("EMBEDDING_SERVICE")

    assert state.state == CBState.OPEN


def test_transition_to_half_open_if_due_leaves_closed_state_unchanged():
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.CLOSED, retry_after=None)
    db.query.return_value.filter.return_value.first.return_value = existing

    state = repo.transition_to_half_open_if_due("EMBEDDING_SERVICE")

    assert state.state == CBState.CLOSED


def test_reset_closes_circuit_and_clears_failure_count():
    db, repo = _repo_with_mock_db()
    existing = MagicMock(state=CBState.OPEN, failure_count=15)
    db.query.return_value.filter.return_value.first.return_value = existing

    state = repo.reset("EMBEDDING_SERVICE")

    assert state.state == CBState.CLOSED
    assert state.failure_count == 0
    assert state.opened_at is None
    assert state.retry_after is None


def test_reset_returns_none_when_no_row_exists():
    db, repo = _repo_with_mock_db()
    db.query.return_value.filter.return_value.first.return_value = None

    assert repo.reset("EMBEDDING_SERVICE") is None
