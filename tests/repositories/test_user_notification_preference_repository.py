from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from app.models.email import EmailTriggerEvent, UserNotificationPreference
from app.repositories.user_notification_preference_repository import UserNotificationPreferenceRepository

"""
Epic 5 Step 3 - MagicMock-based unit tests, matching this project's
universal test-repository convention.
"""


def _repo():
    db = MagicMock()
    return UserNotificationPreferenceRepository(db), db


@contextmanager
def _reraising_savepoint():
    yield


def test_get_all_by_user_id_returns_all_matches():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = rows

    result = repo.get_all_by_user_id("user-1")

    assert result == rows


def test_get_by_user_id_and_trigger_event_returns_first_match():
    repo, db = _repo()
    db.query.return_value.filter.return_value.first.return_value = "the-preference"

    result = repo.get_by_user_id_and_trigger_event("user-1", EmailTriggerEvent.CANDIDATE_SELECTED)

    assert result == "the-preference"


def test_upsert_creates_a_new_row_when_none_exists():
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    db.query.return_value.filter.return_value.first.return_value = None

    result = repo.upsert("user-1", EmailTriggerEvent.CANDIDATE_SELECTED, False)

    assert isinstance(result, UserNotificationPreference)
    assert result.user_id == "user-1"
    assert result.trigger_event == EmailTriggerEvent.CANDIDATE_SELECTED
    assert result.is_enabled is False
    db.add.assert_called_once_with(result)


def test_upsert_updates_the_existing_row_in_place_when_one_exists():
    repo, db = _repo()
    existing = MagicMock(spec=UserNotificationPreference, is_enabled=True)
    db.query.return_value.filter.return_value.first.return_value = existing

    result = repo.upsert("user-1", EmailTriggerEvent.CANDIDATE_SELECTED, False)

    assert result is existing
    assert existing.is_enabled is False
    db.add.assert_not_called()


def test_upsert_applies_this_callers_value_onto_the_winner_on_a_race():
    """
    Two concurrent PUTs for the same (user_id, trigger_event) - the
    loser's flush raises IntegrityError (UNIQUE(user_id, trigger_event)).
    Unlike a hard-lock table, this is a plain toggle: the loser's
    is_enabled value is still applied onto the winner's row rather than
    silently discarded - last write wins.
    """
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    db.flush.side_effect = [IntegrityError("insert", {}, Exception("duplicate key")), None]
    winner_row = MagicMock(spec=UserNotificationPreference, is_enabled=True)
    db.query.return_value.filter.return_value.first.side_effect = [None, winner_row]

    result = repo.upsert("user-1", EmailTriggerEvent.CANDIDATE_SELECTED, False)

    assert result is winner_row
    assert winner_row.is_enabled is False
