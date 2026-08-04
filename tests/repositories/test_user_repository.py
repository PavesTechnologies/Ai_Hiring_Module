from unittest.mock import MagicMock

from app.models.identity import UserRole
from app.repositories.user_repository import UserRepository


def test_get_active_by_role_queries_role_and_active_flag():
    db = MagicMock()
    expected_users = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = expected_users
    repo = UserRepository(db)

    result = repo.get_active_by_role(UserRole.HR_ADMIN)

    assert result == expected_users
    db.execute.assert_called_once()


def test_get_active_by_role_returns_empty_list_when_none_match():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    repo = UserRepository(db)

    result = repo.get_active_by_role(UserRole.HR_ADMIN)

    assert result == []
