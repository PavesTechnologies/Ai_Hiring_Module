"""Against Postgres (rolled back): HR-admin alerts reach a user whose token lists HR_ADMIN after another role."""
import uuid

from app.models.identity import User, UserRole
from app.repositories.user_repository import UserRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _user(db, role, airs_roles, active=True):  # noqa: F811
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.invalid", password_hash="x",
                role=role, airs_roles=airs_roles, full_name="Test", is_active=active)
    db.add(user)
    db.flush()
    return user


def test_primary_and_secondary_hr_admins_are_both_recipients(db):  # noqa: F811
    primary = _user(db, UserRole.HR_ADMIN, ["HR_ADMIN"])
    secondary = _user(db, UserRole.RECRUITER, ["RECRUITER", "HR_ADMIN"])
    recruiter = _user(db, UserRole.RECRUITER, ["RECRUITER"])
    inactive = _user(db, UserRole.RECRUITER, ["RECRUITER", "HR_ADMIN"], active=False)
    ids = {u.id for u in UserRepository(db).get_active_by_role(UserRole.HR_ADMIN)}
    assert {primary.id, secondary.id} <= ids
    assert recruiter.id not in ids and inactive.id not in ids
