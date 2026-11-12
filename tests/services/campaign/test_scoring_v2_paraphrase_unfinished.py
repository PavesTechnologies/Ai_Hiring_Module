"""
Review item 5: a paraphrase round that was capped or failed never lets the first round's
NOT_FOUND decide. Capped is NOT_RUN, failed is PENDING - the same as the first round - so the
deciding path retries or scores without the LLM, and never rejects on the partial result.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.campaign import scoring_v2_service as svc
from app.services.campaign.scoring_v2 import OUTCOME_NOT_RUN, OUTCOME_PENDING
from app.services.campaign.scoring_v2_service import paraphrase_round_outcome
from app.services.skills.core_skill_confirmation_service import CoreSkillSpec

INC = uuid4()


@pytest.mark.parametrize("paraphrase, expected", [
    ({"ran": False, "llm_calls": 0, "skipped_budget": True}, OUTCOME_NOT_RUN),
    ({"ran": True, "llm_calls": 1, "llm_unavailable": True, "problem": "429"}, OUTCOME_PENDING),
    ({"ran": False, "llm_calls": 0, "failed": True, "problem": "RuntimeError: embeddings down"}, OUTCOME_PENDING),
])
def test_an_unfinished_round_is_undecided(paraphrase, expected):
    outcome, reason = paraphrase_round_outcome(paraphrase)
    assert outcome == expected
    assert reason


@pytest.mark.parametrize("paraphrase", [
    {"ran": False, "llm_calls": 0, "targets": [], "proposals": []},     # nothing to retry
    {"ran": False, "llm_calls": 0, "targets": ["SQL"], "proposals": []},  # nothing similar found to ask about
    {"ran": True, "llm_calls": 3, "results": []},                         # finished
])
def test_a_finished_or_empty_round_decides_nothing(paraphrase):
    assert paraphrase_round_outcome(paraphrase) is None


def _service():
    service = svc.ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=lambda: MagicMock())
    service.detection_repository = MagicMock()
    service.evidence_service = MagicMock()
    tools = service.evidence_service.load_tools.return_value
    tools.index.entries = {}
    service.load_config = MagicMock(return_value={"paraphrase": {"enabled": True}})
    row = SimpleNamespace(canonical_skill_id=INC, sense=None, sources=["SCAN"], best_source="SCAN", match_tier="SCAN",
                          recognition=1.0, evidence=[{"text": "line"}], text_hash="h", detection_version=tools.version)
    service.detection_repository.list_evidence.return_value = [row]
    service.db.query.return_value.filter.return_value.all.return_value = []
    return service


def _score(service, paraphrase_effect, llm_fallback):
    first_round = SimpleNamespace(results=[{"canonical_skill_id": str(INC), "status": "NOT_FOUND"}],
                                  skipped_budget=False, llm_unavailable=False, cached=False, problem=None,
                                  llm_calls=3, call=None)
    unavailable, no_llm = MagicMock(), MagicMock()
    campaign = SimpleNamespace(id=uuid4(), deterministic_threshold=50, required_skill_coverage_threshold=60,
                               max_missing_core_skills=3, max_missing_core_pct=None)
    with patch.object(svc, "jd_core_skill_specs", return_value=[CoreSkillSpec(INC, "Incident Management")]), \
            patch.object(svc.CoreSkillConfirmationService, "confirm", return_value=first_round), \
            patch.object(svc.ScoringV2Service, "_paraphrase_round", side_effect=paraphrase_effect), \
            patch.object(svc.ScoringV2Service, "_no_llm_confirmations", return_value={}), \
            patch.object(svc.ScoringV2Service, "provisional_inputs", return_value=[]), \
            patch.object(svc, "jd_paraphrase_sections", return_value=["EXPERIENCE"]), \
            patch.object(svc, "load_mandatory_skill_hierarchy", return_value={}), \
            patch.object(svc, "score_v2", return_value={"outcome": "REJECT", "deterministic_passed": False}), \
            patch.object(svc, "apply_unavailable_outcome", unavailable), \
            patch.object(svc, "apply_no_llm_outcome", no_llm):
        service.score(campaign=campaign, campaign_candidate=MagicMock(), job_description=MagicMock(),
                      resume=SimpleNamespace(id=uuid4(), parsed_json={}), experience_result=None,
                      education_result=None, domain_result=None, acted_on=True, persist=False,
                      llm_fallback=llm_fallback)
    return unavailable, no_llm


CAPPED = [({"ran": False, "llm_calls": 0, "skipped_budget": True, "targets": ["Incident Management"]}, [])]


def test_a_capped_paraphrase_round_is_not_run_where_nothing_falls_back():
    unavailable, no_llm = _score(_service(), CAPPED, svc.FALLBACK_NEVER)
    assert unavailable.call_args.args[1] == OUTCOME_NOT_RUN
    no_llm.assert_not_called()


def test_a_capped_paraphrase_round_on_the_deciding_path_scores_without_the_llm():
    unavailable, no_llm = _score(_service(), CAPPED, svc.FALLBACK_ON_CAP)
    unavailable.assert_not_called()
    assert "paraphrase round" in no_llm.call_args.args[3]


def test_a_failed_paraphrase_round_is_pending_never_the_first_round_result():
    unavailable, no_llm = _score(_service(), RuntimeError("embeddings down"), svc.FALLBACK_ON_CAP)
    outcome, reason = unavailable.call_args.args[1:]
    assert outcome == OUTCOME_PENDING and "embeddings down" in reason
    no_llm.assert_not_called()


def test_a_failed_paraphrase_round_on_the_last_attempt_scores_without_the_llm():
    unavailable, no_llm = _score(_service(), RuntimeError("embeddings down"), svc.FALLBACK_ON_UNAVAILABLE)
    unavailable.assert_not_called()
    no_llm.assert_called_once()


# --- review item 25: score() hands back the row it saved -----------------------------------------

def test_score_records_the_row_it_saved_and_resets_it_each_call():
    service = _service()
    saved = []
    first_round = SimpleNamespace(results=[{"canonical_skill_id": str(INC), "status": "CONFIRMED"}],
                                  skipped_budget=False, llm_unavailable=False, cached=False, problem=None,
                                  llm_calls=3, call=None)
    campaign = SimpleNamespace(id=uuid4(), deterministic_threshold=50, required_skill_coverage_threshold=60,
                               max_missing_core_skills=3, max_missing_core_pct=None)
    repo = MagicMock()
    repo.add.side_effect = saved.append
    breakdown = {"outcome": "PASS", "deterministic_passed": True, "deterministic_score": 80.0}
    kwargs = dict(campaign=campaign, campaign_candidate=SimpleNamespace(id=uuid4()), job_description=MagicMock(),
                  resume=SimpleNamespace(id=uuid4(), parsed_json={}), experience_result=None, education_result=None,
                  domain_result=None, acted_on=False)
    with patch.object(svc, "jd_core_skill_specs", return_value=[CoreSkillSpec(INC, "Incident Management")]), \
            patch.object(svc.CoreSkillConfirmationService, "confirm", return_value=first_round), \
            patch.object(svc.ScoringV2Service, "_paraphrase_round", return_value=({"ran": False, "llm_calls": 0}, [])), \
            patch.object(svc.ScoringV2Service, "provisional_inputs", return_value=[]), \
            patch.object(svc, "jd_paraphrase_sections", return_value=[]), \
            patch.object(svc, "load_mandatory_skill_hierarchy", return_value={}), \
            patch.object(svc, "score_v2", side_effect=lambda *a: dict(breakdown)), \
            patch.object(svc, "ScoringResultRepository", return_value=repo):
        service.score(**kwargs, persist=True)
        assert saved and service.last_result_row is saved[0]
        assert service.last_result_row.outcome == "PASS" and service.last_result_row.acted_on is False
        service.score(**kwargs, persist=False)
        assert service.last_result_row is None
