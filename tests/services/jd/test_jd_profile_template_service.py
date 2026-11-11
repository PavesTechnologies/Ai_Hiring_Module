"""Editing JD profile templates (Phase 3 Step 7): validation, audit, out-of-date marking."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.schemas.jd_profile_template import JDProfileTemplateUpdate
from app.services.jd.jd_profile_template_service import JDProfileTemplateService, template_entity_id

SERVICE = "app.services.jd.jd_profile_template_service"


def _fresher():
    return SimpleNamespace(key="FRESHER", label="Fresher", description="Early career", internship_credit_pct=100,
                           experience_tolerance_years=None, education_requirement="MANDATORY",
                           evidence_sections=["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"],
                           listed_only_can_confirm=True, updated_by=None, updated_at=None)


class FakeRepo:
    def __init__(self, template, live_ids=()):
        self.template, self.live_ids = template, list(live_ids)
        self.committed = self.rolled_back = 0
        self.db = MagicMock()

    def get(self, key):
        return self.template if self.template is not None and key == self.template.key else None

    def live_campaign_ids_using(self, key):
        return list(self.live_ids)

    def flush(self):
        pass

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _request(**kw):
    values = dict(label="Fresher", description="Early career", internship_credit_pct=100,
                  experience_tolerance_years=None, education_requirement="MANDATORY",
                  evidence_sections=["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"], listed_only_can_confirm=True)
    values.update(kw)
    return JDProfileTemplateUpdate(**values)


def _update(repo, request, audit=None):
    audit = audit or MagicMock()
    with patch(f"{SERVICE}.mark_campaigns_out_of_date", return_value=7) as mark:
        result = JDProfileTemplateService(repo, audit).update("FRESHER", request, actor_id="hr-1", actor_role="HR_ADMIN")
    return result, audit, mark


def test_a_scoring_change_is_audited_with_before_after_and_marks_live_campaigns():
    live = uuid4()
    repo = FakeRepo(_fresher(), live_ids=[live])
    (template, changed, marked), audit, mark = _update(repo, _request(internship_credit_pct=50))
    assert changed and marked == 7 and template.internship_credit_pct == 50 and template.updated_by == "hr-1"
    assert mark.call_args.args[1] == [live] and "'Fresher' profile template changed" in mark.call_args.args[2]
    kwargs = audit.log.call_args.kwargs
    assert kwargs["action_type"] == ActionType.JD_PROFILE_TEMPLATE_UPDATED
    assert kwargs["entity_type"] == EntityType.JD_PROFILE_TEMPLATE
    assert kwargs["entity_id"] == template_entity_id("FRESHER") == template_entity_id("FRESHER")
    assert kwargs["details"]["changes"] == {"internship_credit_pct": {"before": 100, "after": 50}}
    assert kwargs["details"]["candidates_marked_out_of_date"] == 7
    assert repo.committed == 1


def test_an_education_or_label_change_marks_nothing():
    # Education on a template only prefills the calibration form; JDs keep their own.
    repo = FakeRepo(_fresher(), live_ids=[uuid4()])
    (_, changed, marked), audit, mark = _update(repo, _request(education_requirement="PREFERRED", label="Graduate"))
    assert changed and marked == 0
    mark.assert_not_called()
    assert set(audit.log.call_args.kwargs["details"]["changes"]) == {"education_requirement", "label"}


def test_a_typed_label_is_audited_as_changed_never_as_text():
    repo = FakeRepo(_fresher())
    (_, changed, _), audit, _ = _update(repo, _request(label="Graduate trainee"))
    assert changed
    assert audit.log.call_args.kwargs["details"]["changes"] == {"label": {"changed": True}}
    assert "Graduate trainee" not in str(audit.log.call_args.kwargs["details"])


def test_no_change_is_not_audited():
    repo = FakeRepo(_fresher())
    (_, changed, _), audit, _ = _update(repo, _request(evidence_sections=["PROJECTS", "SKILLS", "EXPERIENCE", "SUMMARY"]))
    assert not changed                                 # same sections in another order
    audit.log.assert_not_called()
    assert repo.committed == 0


def test_sections_are_stored_in_a_fixed_order():
    repo = FakeRepo(_fresher())
    (template, _, _), _, _ = _update(repo, _request(evidence_sections=["SKILLS", "EXPERIENCE"]))
    assert template.evidence_sections == ["EXPERIENCE", "SKILLS"]


@pytest.mark.parametrize("sections, message", [
    (["EXPERIENCE", "CERTIFICATIONS"], "Unknown resume section"),
    (["SUMMARY", "SKILLS"], "Experience always counts"),
])
def test_sections_are_validated(sections, message):
    repo = FakeRepo(_fresher())
    with pytest.raises(UnprocessableError, match=message):
        _update(repo, _request(evidence_sections=sections))
    assert repo.rolled_back == 1 and repo.template.evidence_sections[-1] == "PROJECTS"


def test_unknown_template():
    with pytest.raises(NotFoundError):
        _update(FakeRepo(None), _request())


def test_turning_off_listed_only_confirmation_is_a_scoring_change():
    live = uuid4()
    repo = FakeRepo(_fresher(), live_ids=[live])
    (template, changed, marked), audit, mark = _update(repo, _request(listed_only_can_confirm=False))
    assert changed and marked == 7 and template.listed_only_can_confirm is False
    assert audit.log.call_args.kwargs["details"]["changes"] == {
        "listed_only_can_confirm": {"before": True, "after": False}}

