"""Against Postgres (rolled back): daily LLM caps and the capability map edited on the Settings page (phase 4)."""
from unittest.mock import MagicMock

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.scoring_v2_service import daily_llm_call_caps
from app.services.llm.capabilities import load_capability_rules
from app.services.settings.llm_settings import LLMSettingsService, assigned_models
from app.repositories.config_repository import ConfigRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _service(db):  # noqa: F811
    audit = MagicMock()
    return LLMSettingsService(db, audit), audit


def test_caps_are_saved_and_read_back_by_scoring_and_audited_as_numbers(db):  # noqa: F811
    service, audit = _service(db)
    in_use = service.models_in_use()
    if not in_use:
        pytest.skip("no model in use")
    model = in_use[0]
    service.save_caps({"default": 250, "models": {model: 120}}, actor_id=None)
    caps = daily_llm_call_caps(ConfigRepository(db))
    assert caps["default"] == 250 and caps[model] == 120
    changes = audit.log.call_args.kwargs["details"]["changes"]
    assert changes["default"]["after"] == 250 and changes[model]["after"] == 120


@pytest.mark.parametrize("payload, problem", [
    ({"default": -1}, "whole number from 0"),
    ({"default": 200001}, "whole number from 0"),
    ({"default": 300, "models": {"gemini-3.1-flash-litee": 10}}, "not a model assigned"),       # a typo
    ({"default": "300"}, "whole number"),
])
def test_invalid_caps_are_refused(db, payload, problem):  # noqa: F811
    service, audit = _service(db)
    with pytest.raises(CampaignException) as exc:
        service.save_caps(payload, actor_id=None)
    assert any(problem in p for p in exc.value.data["problems"]) and not audit.log.called


def test_a_low_cap_is_warned_about(db):  # noqa: F811
    service, _ = _service(db)
    assert service.preview_caps({"default": 10})["warnings"]


def test_the_current_capability_map_is_valid_and_saving_it_unchanged_changes_nothing(db):  # noqa: F811
    service, audit = _service(db)
    rules = service.capabilities()["rules"]
    assert service.save_capabilities({"rules": rules}, actor_id=None)["changed"] is False
    assert not audit.log.called


def test_a_rule_change_is_saved_and_audited_without_its_source_note(db):  # noqa: F811
    service, audit = _service(db)
    rules = [dict(r) for r in service.capabilities()["rules"]]
    rules[0]["max_output_tokens"] = 4096
    rules[0]["source"] = "a free-text note"
    service.save_capabilities({"rules": rules}, actor_id=None)
    details = audit.log.call_args.kwargs["details"]
    assert details["rules_changed"] and "free-text" not in str(details)
    assert load_capability_rules(db)[0]["max_output_tokens"] == 4096


@pytest.mark.parametrize("mutate, problem", [
    (lambda r: r[0].update(provider="MISTRAL"), "provider must be one of"),
    (lambda r: r[0].update(structured_output="xml"), "structured_output for"),
    (lambda r: r[0].update(max_output_tokens=10), "max_output_tokens"),
    (lambda r: r[0].update(supports_temperature="yes"), "true or false"),
    (lambda r: r[0].update(colour="blue"), "unknown field"),
])
def test_invalid_rules_are_refused(db, mutate, problem):  # noqa: F811
    service, _ = _service(db)
    rules = [dict(r) for r in service.capabilities()["rules"]]
    mutate(rules)
    with pytest.raises(CampaignException) as exc:
        service.save_capabilities({"rules": rules}, actor_id=None)
    assert any(problem in p for p in exc.value.data["problems"])


def test_a_map_that_drops_an_assigned_model_is_refused(db):  # noqa: F811
    service, _ = _service(db)
    assigned = assigned_models(db)
    if not assigned:
        pytest.skip("no model assigned on the AI providers tab")
    provider = assigned[0][0]
    rules = [r for r in service.capabilities()["rules"] if str(r["provider"]).upper() != provider]
    with pytest.raises(CampaignException) as exc:
        service.save_capabilities({"rules": rules or [{"provider": "OPENAI", "model_pattern": "gpt-*",
                                                       "supports_temperature": False,
                                                       "structured_output": "json_schema"}]}, actor_id=None)
    assert any("is assigned on the AI providers tab" in p for p in exc.value.data["problems"])
