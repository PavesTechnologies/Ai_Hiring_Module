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


def test_unlock_reasons_are_listed_apart_and_counted_from_the_unlock_audits(db):  # noqa: F811
    from uuid import uuid4
    from app.models.compliance import AuditLog
    service, _ = _service(db)
    before = {r["code"]: r["times_used"] for r in service.list("STAGE_UNLOCK")}
    assert set(before) >= {"EMAIL_SENT_IN_ERROR", "CANDIDATE_REENGAGED", "DATA_CORRECTION"}
    assert not set(before) & set(outcome_overrides.DEFAULT_REASON_CODES)
    db.add(AuditLog(action_type="STAGE_UNLOCKED", entity_type="CAMPAIGN_CANDIDATE", entity_id=uuid4(),
                    detail={"reason_code": "DATA_CORRECTION"}))
    db.flush()
    after = {r["code"]: r["times_used"] for r in service.list("STAGE_UNLOCK")}
    assert after["DATA_CORRECTION"] == before["DATA_CORRECTION"] + 1


def test_an_added_unlock_reason_is_offered_for_unlocking_not_for_overrides(db):  # noqa: F811
    from app.services.campaign import stage_lock
    service, audit = _service(db)
    rows = service.create({"code": "DUPLICATE_PROFILE", "label": "Duplicate profile"}, actor_id=None,
                          kind="STAGE_UNLOCK")
    assert "DUPLICATE_PROFILE" in {r["code"] for r in rows}
    assert stage_lock.unlock_reason_codes(db)["DUPLICATE_PROFILE"] == "Duplicate profile"
    assert "DUPLICATE_PROFILE" not in outcome_overrides.reason_codes(db, active_only=False)
    assert audit.log.call_args.kwargs["details"]["kind"] == "STAGE_UNLOCK"


def test_the_last_active_unlock_reason_cant_be_retired(db):  # noqa: F811
    service, _ = _service(db)
    active = [r["code"] for r in service.list("STAGE_UNLOCK") if r["is_active"]]
    for code in active[:-1]:
        service.update(code, {"is_active": False}, actor_id=None)
    with pytest.raises(CampaignException) as refused:
        service.update(active[-1], {"is_active": False}, actor_id=None)
    assert refused.value.status_code == 422
    service.update(active[0], {"is_active": True}, actor_id=None)           # restoring one is fine
