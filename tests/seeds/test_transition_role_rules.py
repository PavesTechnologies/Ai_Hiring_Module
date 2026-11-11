"""2026-11-11: HR_ADMIN holds every RECRUITER-owned move (bulk move and reject), SYSTEM every automated move."""
from types import SimpleNamespace

from app.models.pipeline import PipelineStage as S
from app.seeds.transition_role_rules import SYSTEM_EDGES, reconcile_existing, required_roles
from app.services.campaign.stage_transition_service import _FULL_SCORING_HOLD_MOVES, _SYSTEM_MOVES


def test_a_recruiter_owned_move_gains_hr_admin():
    assert required_roles(None, S.SHORTLISTED, S.REJECTED, ["RECRUITER"]) == ["RECRUITER", "HR_ADMIN"]


def test_a_hiring_manager_owned_move_stays_the_hiring_managers():
    assert required_roles(S.HM_REVIEW, S.SHORTLISTED, S.REJECTED, ["HIRING_MANAGER"]) == ["HIRING_MANAGER"]


def test_automated_moves_gain_system_only_on_the_rows_scoring_uses():
    assert required_roles(None, S.SCREENING, S.HOLD, ["RECRUITER"]) == ["RECRUITER", "HR_ADMIN", "SYSTEM"]
    assert "SYSTEM" in required_roles(S.SCREENING, S.HOLD, S.REJECTED, ["RECRUITER"])
    assert "SYSTEM" not in required_roles(S.INTERVIEW, S.HOLD, S.REJECTED, ["RECRUITER"])   # not a fallback HOLD


def test_roles_are_only_added_never_reordered_or_removed():
    assert required_roles(None, S.SCREENING, S.REJECTED, ["SYSTEM", "RECRUITER", "HR_ADMIN"]) == \
        ["SYSTEM", "RECRUITER", "HR_ADMIN"]


def test_the_rule_covers_every_move_the_code_makes_as_system():
    """If scoring gains an automated move, the rule (and the rows) must say so too."""
    assert set(SYSTEM_EDGES) == set(_SYSTEM_MOVES) | set(_FULL_SCORING_HOLD_MOVES)


def test_reconcile_reports_only_rows_it_changed():
    rows = [SimpleNamespace(previous_stage=None, from_stage=S.SCREENING, to_stage=S.UPLOADED, allowed_roles=["RECRUITER"]),
            SimpleNamespace(previous_stage=None, from_stage=S.UPLOADED, to_stage=S.SCREENING, allowed_roles=["SYSTEM"])]
    db = SimpleNamespace(query=lambda model: SimpleNamespace(all=lambda: rows))
    assert reconcile_existing(db) == [("ANY", "SCREENING", "UPLOADED", ["HR_ADMIN"])]
    assert rows[0].allowed_roles == ["RECRUITER", "HR_ADMIN"]
