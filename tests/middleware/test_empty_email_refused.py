"""
Deferred finding A-M2: a token with an empty email is refused - never used to create a local user or
to re-key another user's row by email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.exception_handler.exceptions import ForbiddenError
from app.middleware import rbac


@pytest.mark.parametrize("email", ["", "   ", None])
def test_an_empty_email_is_refused_before_any_write(email):
    db = MagicMock()
    db.get.return_value = None
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(id="someone-else")
    token = rbac.TokenUser(user_id="new-id", email=email, roles=["RECRUITER"])
    with pytest.raises(ForbiddenError):
        rbac._ensure_local_user(token, db)
    db.add.assert_not_called()
    db.execute.assert_not_called()          # no re-key of the other row
    db.commit.assert_not_called()


def test_a_real_email_still_provisions():
    db = MagicMock()
    db.get.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    rbac._ensure_local_user(rbac.TokenUser(user_id="u", email="pat@example.invalid", roles=["RECRUITER"]), db)
    db.add.assert_called_once()
    db.commit.assert_called_once()
