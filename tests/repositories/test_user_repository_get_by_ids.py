from unittest.mock import MagicMock

from app.repositories.user_repository import UserRepository


def _make_repo(users=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = users or []
    return UserRepository(db), db


def test_get_by_ids_returns_empty_list_without_querying_when_no_ids():
    repo, db = _make_repo()

    result = repo.get_by_ids([])

    assert result == []
    db.execute.assert_not_called()


def test_get_by_ids_returns_matching_users():
    user = MagicMock(id="user-1", full_name="Jane Recruiter")
    repo, db = _make_repo([user])

    result = repo.get_by_ids(["user-1"])

    assert result == [user]
    db.execute.assert_called_once()
