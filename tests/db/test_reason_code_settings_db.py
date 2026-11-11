"""Against Postgres (rolled back): override reason codes edited on the Settings page."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.services.campaign import outcome_overrides
from app.services.settings.reason_code_settings import ReasonCodeSettingsService
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _service(db):  # noqa: F811
    audit = MagicMock()
    return ReasonCodeSettingsService(db, audit), audit


def test_the_migrated_codes_are_the_former_built_in_list(db):  # noqa: F811
    service, _ = _service(db)
    rows = service.list()
    assert [r["code"] for r in rows] == list(outcome_overrides.DEFAULT_REASON_CODES)
    assert all(r["is_active"] for r in rows) and [r["code"] for r in rows if r["locked"]] == ["OTHER"]


def test_a_new_code_is_offered_and_audited_without_its_label(db):  # noqa: F811
    service, audit = _service(db)
    service.create({"code": "salary_expectation", "label": "Salary expectation"}, actor_id=None)
    assert outcome_overrides.reason_codes(db)["SALARY_EXPECTATION"] == "Salary expectation"
    details = audit.log.call_args.kwargs["details"]
    assert details["code"] == "SALARY_EXPECTATION" and "Salary expectation" not in str(details)


def test_a_relabel_is_audited_as_changed_and_a_retired_code_is_no_longer_accepted(db):  # noqa: F811
    service, audit = _service(db)
    service.update("ROLE_FIT", {"label": "Fit for the role", "is_active": False}, actor_id=None)
    assert audit.log.call_args.kwargs["details"]["changes"] == {
        "label": {"changed": True}, "is_active": {"before": True, "after": False}}
    assert "ROLE_FIT" not in outcome_overrides.reason_codes(db)
    assert outcome_overrides.reason_codes(db, active_only=False)["ROLE_FIT"] == "Fit for the role"  # history


def test_check_move_refuses_a_retired_code(db, monkeypatch):  # noqa: F811
    service, _ = _service(db)
    service.update("ROLE_FIT", {"is_active": False}, actor_id=None)
    monkeypatch.setattr(outcome_overrides, "acted_on_result",
                        lambda db, ccid: SimpleNamespace(outcome="PASS", breakdown={}, id=None, scoring_version="v2",
                                                         scoring_config_version="t"))
    cc = SimpleNamespace(id=None, campaign_id=None)
    with pytest.raises(CampaignException) as exc:
        outcome_overrides.check_move(db, cc, PipelineStage.SCREENING, PipelineStage.REJECTED,
                                     actor_id="u", actor_role="RECRUITER", reason_code="ROLE_FIT")
    assert exc.value.data["code"] == outcome_overrides.UNKNOWN_CODE
    assert "ROLE_FIT" not in {c["code"] for c in exc.value.data["reason_codes"]}


@pytest.mark.parametrize("call, status", [
    (lambda s: s.update("OTHER", {"is_active": False}, actor_id=None), 422),        # always available
    (lambda s: s.update("ROLE_FIT", {"code": "FIT"}, actor_id=None), 422),          # never renamed
    (lambda s: s.update("NOPE", {"label": "x y"}, actor_id=None), 404),
    (lambda s: s.update("ROLE_FIT", {"label": " "}, actor_id=None), 422),
    (lambda s: s.create({"code": "ROLE_FIT", "label": "Again"}, actor_id=None), 409),
    (lambda s: s.create({"code": "x", "label": "Too short code"}, actor_id=None), 422),
    (lambda s: s.create({"code": "NEW_CODE", "label": "Order", "sort_order": -1}, actor_id=None), 422),
])
def test_refused_changes_write_nothing(db, call, status):  # noqa: F811
    service, audit = _service(db)
    before = service.list()
    with pytest.raises(CampaignException) as exc:
        call(service)
    assert exc.value.status_code == status
    assert service.list() == before and not audit.log.called


def test_no_change_is_not_audited(db):  # noqa: F811
    service, audit = _service(db)
    service.update("ROLE_FIT", {"label": "Role fit"}, actor_id=None)
    assert not audit.log.called
