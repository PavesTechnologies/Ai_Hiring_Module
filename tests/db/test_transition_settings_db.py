"""Against Postgres (rolled back): stage-transition changes from the Settings page, on dev's real table."""
from unittest.mock import MagicMock

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import AllowedTransition, PipelineStage as S
from app.services.settings.transition_settings import TransitionSettingsService
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _service(db):  # noqa: F811
    audit = MagicMock()
    return TransitionSettingsService(db, audit), audit


def _find(listing, frm, to, prev=None):
    return next(t for t in listing["transitions"]
                if (t["from_stage"], t["to_stage"], t["previous_stage"]) == (frm, to, prev))


def _count(db):  # noqa: F811
    return db.query(AllowedTransition).count()


def test_the_listing_marks_protected_and_system_moves(db):  # noqa: F811
    service, _ = _service(db)
    listing = service.list()
    assert _find(listing, "SCREENING", "HOLD")["system"] is True
    assert _find(listing, "SCREENING", "HOLD")["protected"]
    assert _find(listing, "SHORTLISTED", "INTERVIEW")["protected"] is None
    assert listing["terminal_stages"] == ["REJECTED", "SELECTED"]


def test_changing_roles_and_reason_is_saved_and_audited_keeping_system(db):  # noqa: F811
    service, audit = _service(db)
    row = _find(service.list(), "SCREENING", "HOLD")
    service.save([{"op": "update", "id": row["id"], "allowed_roles": ["RECRUITER"], "requires_reason": True}],
                 actor_id=None)
    after = _find(service.list(), "SCREENING", "HOLD")
    assert after["allowed_roles"] == ["RECRUITER", "SYSTEM"] and after["requires_reason"] is True
    entry = audit.log.call_args.kwargs["details"]["changes"][0]
    assert entry["allowed_roles"] == {"before": ["RECRUITER", "HR_ADMIN", "SYSTEM"], "after": ["RECRUITER", "SYSTEM"]}
    assert entry["requires_reason"] == {"before": False, "after": True}


@pytest.mark.parametrize("change_for", [
    lambda l: {"op": "remove", "id": _find(l, "SCREENING", "HOLD")["id"]},                       # automated
    lambda l: {"op": "remove", "id": _find(l, "REJECTED", "SCREENING", "HOLD")["id"]},           # re-parse
    lambda l: {"op": "update", "id": _find(l, "SCREENING", "HOLD")["id"], "allowed_roles": ["SYSTEM"]},
    lambda l: {"op": "update", "id": _find(l, "SHORTLISTED", "INTERVIEW")["id"], "allowed_roles": ["GUEST"]},
    lambda l: {"op": "update", "id": _find(l, "SHORTLISTED", "INTERVIEW")["id"], "allowed_roles": []},
    lambda l: {"op": "add", "from_stage": "SELECTED", "to_stage": "SHORTLISTED", "allowed_roles": ["RECRUITER"]},
    lambda l: {"op": "add", "from_stage": "REJECTED", "to_stage": "INTERVIEW", "allowed_roles": ["HR_ADMIN"]},
    lambda l: {"op": "add", "from_stage": "SHORTLISTED", "to_stage": "INTERVIEW", "allowed_roles": ["HR_ADMIN"]},
])
def test_changes_that_break_a_rule_are_refused_and_nothing_is_written(db, change_for):  # noqa: F811
    service, audit = _service(db)
    listing, before = service.list(), _count(db)
    with pytest.raises(CampaignException) as exc:
        service.save([change_for(listing)], actor_id=None)
    assert exc.value.status_code == 422
    assert _count(db) == before and service.list() == listing and not audit.log.called


def test_removing_every_way_into_a_stage_is_refused(db):  # noqa: F811
    service, _ = _service(db)
    listing = service.list()
    into_selected = [{"op": "remove", "id": t["id"]} for t in listing["transitions"] if t["to_stage"] == "SELECTED"]
    with pytest.raises(CampaignException) as exc:
        service.save(into_selected, actor_id=None)
    assert any("Selected could no longer be reached" in p for p in exc.value.data["problems"])


def test_a_batch_is_all_or_nothing(db):  # noqa: F811
    service, _ = _service(db)
    listing = service.list()
    good = {"op": "update", "id": _find(listing, "SHORTLISTED", "INTERVIEW")["id"], "requires_reason": True}
    bad = {"op": "remove", "id": _find(listing, "SCREENING", "HOLD")["id"]}
    with pytest.raises(CampaignException):
        service.save([good, bad], actor_id=None)
    assert _find(service.list(), "SHORTLISTED", "INTERVIEW")["requires_reason"] is False


def test_a_new_move_and_its_removal_are_saved(db):  # noqa: F811
    service, audit = _service(db)
    before = _count(db)
    service.save([{"op": "add", "previous_stage": "SCREENING", "from_stage": "HM_REVIEW", "to_stage": "SELECTED",
                   "allowed_roles": ["HIRING_MANAGER"], "requires_reason": True}], actor_id=None)
    assert _count(db) == before + 1
    new = _find(service.list(), "HM_REVIEW", "SELECTED", "SCREENING")
    assert audit.log.call_args.kwargs["details"]["changes"][0]["op"] == "add"
    service.save([{"op": "remove", "id": new["id"]}], actor_id=None)
    assert _count(db) == before


def test_preview_lists_the_changes_and_saves_nothing(db):  # noqa: F811
    service, audit = _service(db)
    row = _find(service.list(), "SHORTLISTED", "INTERVIEW")
    result = service.preview([{"op": "update", "id": row["id"], "requires_reason": True}])
    assert result["changes"][0]["requires_reason"] == {"before": False, "after": True}
    assert _find(service.list(), "SHORTLISTED", "INTERVIEW")["requires_reason"] is False and not audit.log.called


def test_the_real_pipeline_still_uses_an_edited_row(db):  # noqa: F811
    """The move stays allowed for the roles kept, and refused for the role removed."""
    from app.repositories.allowed_transition_repository import AllowedTransitionRepository
    service, _ = _service(db)
    row = _find(service.list(), "SHORTLISTED", "INTERVIEW")
    service.save([{"op": "update", "id": row["id"], "allowed_roles": ["RECRUITER"]}], actor_id=None)
    edge = AllowedTransitionRepository(db).get(S.SHORTLISTED, S.INTERVIEW, previous_stage=None)
    assert "RECRUITER" in edge.allowed_roles and "HR_ADMIN" not in edge.allowed_roles
