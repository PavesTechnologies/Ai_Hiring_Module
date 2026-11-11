"""Against the real allowed_transitions rows: an HR admin's bulk move and reject now pass the edge check."""
from app.models.pipeline import AllowedTransition, PipelineStage as S
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.seeds.transition_role_rules import required_roles
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def test_hr_admin_may_make_the_recruiter_moves_bulk_move_and_reject_use(db):  # noqa: F811
    repo = AllowedTransitionRepository(db)
    for from_stage, to_stage in [(S.SCREENING, S.REJECTED), (S.SCREENING, S.SHORTLISTED),
                                 (S.SHORTLISTED, S.REJECTED), (S.SHORTLISTED, S.INTERVIEW),
                                 (S.INTERVIEW, S.REJECTED), (S.INTERVIEW, S.SELECTED)]:
        row = repo.get(from_stage, to_stage, previous_stage=None)
        assert row is not None and "HR_ADMIN" in row.allowed_roles, (from_stage, to_stage)


def test_every_row_already_holds_the_roles_the_rule_requires(db):  # noqa: F811
    for row in db.query(AllowedTransition).all():
        assert required_roles(row.previous_stage, row.from_stage, row.to_stage, row.allowed_roles) == \
            list(row.allowed_roles), (row.previous_stage, row.from_stage, row.to_stage)


def test_hiring_manager_decisions_stay_the_hiring_managers(db):  # noqa: F811
    for row in db.query(AllowedTransition).filter(AllowedTransition.previous_stage == S.HM_REVIEW).all():
        if "RECRUITER" not in row.allowed_roles:
            assert "HR_ADMIN" not in row.allowed_roles
