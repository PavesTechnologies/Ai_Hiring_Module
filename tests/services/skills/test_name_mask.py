"""The candidate's name never reaches the confirmation and paraphrase voters."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.skills.core_skill_confirmation_service import (
    CoreSkillCheckItem, CoreSkillCheckResponse, CoreSkillConfirmationService, CoreSkillSpec, _Ask, evidence_hash,
)
from app.services.skills.name_mask import PLACEHOLDER, NameMask

INC = uuid4()


@pytest.mark.parametrize("text, masked", [
    ("PRIYA RAMAN — Senior Data Engineer", "[candidate] — Senior Data Engineer"),
    ("Priya  Raman led the incident bridge", "[candidate] led the incident bridge"),
    ("Raman's team ran the major incident process", "[candidate]'s team ran the major incident process"),
    ("Priya built the ITSM incident workflows", "[candidate] built the ITSM incident workflows"),
    ("Built incident workflows for the Raman Group", "Built incident workflows for the [candidate] Group"),
    ("raman noodles are not a name here", "raman noodles are not a name here"),        # lower case: not a name
])
def test_the_full_name_and_its_parts_written_as_a_name_are_masked(text, masked):
    assert NameMask.build(["Priya Raman"]).apply(text) == masked


def test_a_name_part_that_is_a_skill_is_masked_only_as_part_of_the_full_name():
    mask = NameMask.build(["Ruby Sharma"], catalog_terms=["Ruby", "Python"])
    assert mask.apply("Ruby Sharma — Backend Developer") == "[candidate] — Backend Developer"
    assert mask.apply("Built APIs in Ruby and Python") == "Built APIs in Ruby and Python"
    assert mask.apply("Sharma built the Rails services") == "[candidate] built the Rails services"


def test_no_name_masks_nothing():
    assert NameMask.build([]).apply("Priya Raman") == "Priya Raman"
    assert NameMask.for_resume(SimpleNamespace(parsed_json={})).apply("Priya Raman") == "Priya Raman"
    assert NameMask.for_resume(MagicMock()).apply("Priya Raman") == "Priya Raman"           # no string name
    assert NameMask.for_resume(SimpleNamespace(parsed_json={"full_name": "Priya Raman"})).apply(
        "Priya Raman") == PLACEHOLDER


class _RecordingProvider:
    """Answers yes with a quote from the (masked) excerpt; records every prompt."""
    provider_name, model = "test", "test-model"

    def __init__(self):
        self.prompts = []


def _answer(prompt):
    return CoreSkillCheckResponse(items=[CoreSkillCheckItem(number=1, present=True,
                                                             quote="[candidate] ran the major incident bridge")])


def test_the_voters_never_see_the_name_and_quotes_check_against_the_masked_text():
    provider = _RecordingProvider()
    row = {"canonical_skill_id": INC, "sense": None, "sources": ["SCAN"], "evidence": [
        {"text": "Priya Raman ran the major incident bridge for 40 stores", "source": "SCAN", "section": "SUMMARY"}]}
    spec = CoreSkillSpec(INC, "Incident Management")

    def fake_call(prov, prompt, schema, **kwargs):
        prov.prompts.append(prompt)
        kwargs["check"](_answer(prompt))
        return SimpleNamespace(value=_answer(prompt), call=SimpleNamespace(as_dict=lambda: {"provider": "test",
                               "model": "test-model", "used_fallback": False}), problem=None, attempts=1)

    with patch("app.services.skills.core_skill_confirmation_service.structured_call", side_effect=fake_call):
        outcome = CoreSkillConfirmationService(provider).confirm(
            resume_id=uuid4(), jd_id=uuid4(), core_skills=[spec], evidence_rows=[row], text_hash="h",
            persist=False, use_cache=False, name_mask=NameMask.build(["Priya Raman"]))
    assert provider.prompts and all("Priya" not in p and "Raman" not in p for p in provider.prompts)
    assert all("[candidate] ran the major incident bridge" in p for p in provider.prompts)
    assert outcome.results[0]["status"] == "CONFIRMED"                     # the masked quote was found


def test_the_evidence_hash_changes_only_when_a_name_was_there():
    mask = NameMask.build(["Priya Raman"])
    spec = CoreSkillSpec(INC, "Incident Management")
    plain = "Ran the major incident bridge"
    named = "Priya Raman ran the major incident bridge"
    assert evidence_hash(_Ask(1, spec, [("Summary", mask.apply(plain))], ["SCAN"])) == \
        evidence_hash(_Ask(1, spec, [("Summary", plain)], ["SCAN"]))
    assert mask.apply(named) != named


class Stop(BaseException):
    """Not an Exception: scoring's own error handling must not swallow it."""


def test_scoring_passes_the_resume_name_to_the_confirmation_round():
    from app.services.campaign import scoring_v2_service as svc
    captured = []

    def fake_confirm(self, **kwargs):
        captured.append(kwargs.get("name_mask"))
        raise Stop

    service = svc.ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=lambda: MagicMock())
    service.detection_repository = MagicMock()
    service.evidence_service = MagicMock()
    service.evidence_service.load_tools.return_value.index.entries = {"python": []}
    service.load_config = MagicMock(return_value={})
    row = SimpleNamespace(canonical_skill_id=INC, sense=None, sources=["SCAN"], best_source="SCAN", match_tier="SCAN",
                          recognition=1.0, evidence=[{"text": "line"}], text_hash="h",
                          detection_version=service.evidence_service.load_tools.return_value.version)
    service.detection_repository.list_evidence.return_value = [row]
    service.db.query.return_value.filter.return_value.all.return_value = []
    resume = SimpleNamespace(id=uuid4(), parsed_json={"full_name": "Priya Raman"})
    with patch.object(svc, "jd_core_skill_specs", return_value=[CoreSkillSpec(INC, "Incident Management")]), \
            patch.object(svc.CoreSkillConfirmationService, "confirm", fake_confirm), pytest.raises(Stop):
        service.score(campaign=MagicMock(), campaign_candidate=MagicMock(), job_description=MagicMock(),
                      resume=resume, experience_result=None, education_result=None, domain_result=None,
                      acted_on=False, persist=False)
    assert captured and captured[0].apply("Priya Raman") == PLACEHOLDER


def test_the_paraphrase_round_passes_the_name_mask_on():
    from app.services.campaign import scoring_v2_service as svc
    from app.services.skills.paraphrase_proposal_service import Proposal
    captured = []

    def fake_confirm(self, **kwargs):
        captured.append(kwargs.get("name_mask"))
        raise Stop

    service = svc.ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=lambda: MagicMock())
    service.db.query.return_value.filter.return_value.all.return_value = []
    spec = CoreSkillSpec(INC, "Incident Management")
    proposal = Proposal(INC, "Priya Raman ran the major incident bridge", 3, 0.6, "EXPERIENCE")
    proposer = MagicMock()
    proposer.return_value.propose.return_value = ([proposal], {})
    mask = NameMask.build(["Priya Raman"])
    with patch.object(svc, "_paraphrase_targets_for", return_value=[MagicMock(name="target")]), \
            patch.object(svc, "redacted_resume_text", return_value="EXPERIENCE\nPriya Raman ran the bridge"), \
            patch.object(svc, "ParaphraseProposalService", proposer), \
            patch("app.services.ai.embedding_service.EmbeddingService"), \
            patch.object(svc.CoreSkillConfirmationService, "confirm", fake_confirm), pytest.raises(Stop):
        service._paraphrase_round(resume=SimpleNamespace(id=uuid4(), parsed_json={}), job_description=MagicMock(),
                                  specs=[spec], confirmations={}, evidence_rows=[], tools=MagicMock(), config={},
                                  text_hash="h", persist=False, use_cache=False, name_mask=mask)
    assert captured == [mask]
