"""
Deferred finding A-M3: a user with no AIRS role in the token gets none locally (never a default
RECRUITER), and the local role follows the token on every AIRS request - including a refused one, so a
demoted HR admin stops getting HR-admin alerts.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.exception_handler.exceptions import ForbiddenError
from app.middleware import rbac
from app.models.identity import UserRole


def _token(roles, email="pat@example.invalid", user_id="u-1"):
    return rbac.TokenUser(user_id=user_id, email=email, roles=list(roles), claims={"name": "Pat"})


def test_a_new_user_without_an_airs_role_gets_none():
    assert rbac._new_user_from_token(_token(["General"])).role is None
    assert rbac._new_user_from_token(_token(["General", "HR_ADMIN"])).role == UserRole.HR_ADMIN


@pytest.mark.parametrize("before, roles, after", [
    (UserRole.HR_ADMIN, ["RECRUITER"], UserRole.RECRUITER),     # demoted
    (UserRole.RECRUITER, ["General"], None),                    # no AIRS role any more
    (None, ["HIRING_MANAGER"], UserRole.HIRING_MANAGER),        # granted one
])
def test_the_local_role_follows_the_token(before, roles, after):
    local = SimpleNamespace(role=before)
    db = MagicMock()
    db.get.return_value = local
    rbac._ensure_local_user(_token(roles), db)
    assert local.role == after
    db.commit.assert_called_once()


def test_an_unchanged_role_writes_nothing():
    db = MagicMock()
    db.get.return_value = SimpleNamespace(role=UserRole.RECRUITER)
    rbac._ensure_local_user(_token(["RECRUITER"]), db)
    db.commit.assert_not_called()


def test_a_refused_request_still_refreshes_the_role(monkeypatch):
    local = SimpleNamespace(role=UserRole.HR_ADMIN)
    db = MagicMock()
    db.get.return_value = local
    monkeypatch.setattr(rbac, "_get_payload", lambda request: {})
    monkeypatch.setattr(rbac, "_to_token_user", lambda payload: _token(["RECRUITER"]))
    check = rbac.require_roles(UserRole.HR_ADMIN)
    with pytest.raises(ForbiddenError):
        check(request=MagicMock(), db=db)
    assert local.role == UserRole.RECRUITER          # an ex-admin no longer counts as one
