"""Monitoring: LLM calls are tied to their resume and campaign, and priced at the seeded list prices."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.llm import call_log
from app.services.monitoring.llm_prices import (
    DEFAULT_LLM_MODEL_PRICES, InvalidPrices, estimate_cost, load_prices, validate_prices,
)


def test_a_call_is_logged_against_the_resume_and_campaign_it_was_for():
    rows = []
    resume_id, campaign_id = uuid4(), uuid4()
    with patch.object(call_log, "_recorder", rows.append):
        with call_log.llm_call_subject(campaign_id=campaign_id):
            with call_log.llm_call_subject(resume_id=resume_id):          # inner adds, outer kept
                call_log.record_call(provider="GOOGLE", model="m", purpose=None, outcome="ok", latency_ms=1)
        call_log.record_call(provider="GOOGLE", model="m", purpose=None, outcome="ok", latency_ms=1)
    assert (rows[0]["resume_id"], rows[0]["campaign_id"]) == (resume_id, campaign_id)
    assert (rows[1]["resume_id"], rows[1]["campaign_id"]) == (None, None)


def test_scoring_ties_its_calls_to_the_resume_and_campaign():
    from app.services.campaign.scoring_v2_service import ScoringV2Service
    service = ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=None)
    seen = {}
    resume, campaign = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())

    def fake_score(**kwargs):
        seen.update(call_log.current_subject())
        return {"outcome": "PASS"}

    with patch.object(service, "_score", side_effect=fake_score):
        assert service.score(campaign=campaign, campaign_candidate=MagicMock(), job_description=MagicMock(),
                             resume=resume, experience_result=None, education_result=None, domain_result=None,
                             acted_on=False) == {"outcome": "PASS"}
    assert seen == {"resume_id": resume.id, "campaign_id": campaign.id}
    assert call_log.current_subject() == {}


def test_the_seeded_prices_are_valid_and_carry_their_source():
    prices = validate_prices(DEFAULT_LLM_MODEL_PRICES)
    assert prices["models"]["gemini-3.1-flash-lite"] == {
        "input": 0.25, "output": 1.5, "source": DEFAULT_LLM_MODEL_PRICES["models"]["gemini-3.1-flash-lite"]["source"]}
    assert "ai.google.dev/gemini-api/docs/pricing" in prices["models"]["gemini-3.5-flash-lite"]["source"]


@pytest.mark.parametrize("payload, problem", [
    ({}, "Give the prices"),
    ({"models": {"m": {"input": -1, "output": 1}}}, "input price"),
    ({"models": {"m": {"input": 1, "output": "cheap"}}}, "output price"),
    ({"models": {"m": {"input": 1, "output": 5000}}}, "output price"),
    ({"models": {"": {"input": 1, "output": 1}}}, "needs a name"),
])
def test_invalid_prices_are_refused(payload, problem):
    with pytest.raises(InvalidPrices, match=problem):
        validate_prices(payload)


def test_cost_is_an_estimate_at_list_price_and_unknown_models_are_unpriced():
    prices = validate_prices(DEFAULT_LLM_MODEL_PRICES)
    assert estimate_cost(prices, "gemini-3.1-flash-lite", 1_000_000, 200_000) == pytest.approx(0.25 + 0.3)
    assert estimate_cost(prices, "some-other-model", 1000, 1000) is None


def test_unreadable_stored_prices_mean_no_prices():
    repo = MagicMock()
    repo.get_configs_by_keys.return_value = {"LLM_MODEL_PRICES": "not json"}
    assert load_prices(repo)["models"] == {}


def test_a_price_change_is_audited_with_numbers_and_never_the_source_text():
    from app.services.monitoring.llm_prices import price_changes
    before = {"models": {"m1": {"input": 0.25, "output": 1.5, "source": "old page"},
                         "m2": {"input": 1.0, "output": 2.0}}}
    after = {"models": {"m1": {"input": 0.30, "output": 1.5, "source": "new page"},
                        "m3": {"input": 0.1, "output": 0.2, "source": "x"}}}
    details = price_changes(before, after)
    assert details == {"models_added": ["m3"], "models_removed": ["m2"],
                       "models_changed": {"m1": {"input": {"before": 0.25, "after": 0.30},
                                                 "source": {"changed": True}}}}
    assert "page" not in str(details)

