"""
Step 3 (extraction model comparison) support: Gemini reports each call's token
usage; a provider can be built for an explicit model with the saved key and no
backup; v2 can score a run's own evidence without the confirmation cache.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.llm.factory import resolve_provider_model
from app.services.llm.fallback_provider import FallbackLLMProvider
from app.services.llm.gemini_provider import GeminiProvider


def _gemini(response):
    with patch("app.services.llm.gemini_provider.genai.Client") as client:
        client.return_value.models.generate_content.return_value = response
        provider = GeminiProvider("key", "gemini-3.8-flash")
    return provider


def test_gemini_records_the_last_calls_token_usage():
    usage = SimpleNamespace(prompt_token_count=4000, candidates_token_count=900, thoughts_token_count=600)
    provider = _gemini(SimpleNamespace(text='{"ok": true}', usage_metadata=usage))
    assert provider.last_usage is None
    provider.generate_json("prompt", MagicMock())
    assert provider.last_usage == {"input": 4000, "output": 900, "thinking": 600}


def test_gemini_without_usage_metadata_records_none():
    provider = _gemini(SimpleNamespace(text='{"ok": true}', usage_metadata=None))
    provider.generate_json("prompt", MagicMock())
    assert provider.last_usage is None


def test_an_explicit_model_uses_the_saved_key_and_has_no_backup():
    config = SimpleNamespace(provider="GOOGLE", is_verified=True, api_key_encrypted=b"x", encryption_key_id="k1")
    with patch("app.services.llm.factory.AIProviderConfigRepository") as repo, \
            patch("app.services.llm.factory.EncryptionService") as encryption, \
            patch("app.services.llm.factory.load_capability_rules", return_value=[]), \
            patch("app.services.llm.factory.build_provider") as build:
        repo.return_value.get_by_provider.return_value = config
        encryption.return_value.decrypt.return_value = "saved-key"
        provider = resolve_provider_model(MagicMock(), "GOOGLE", "gemini-3.8-flash")

    build.assert_called_once_with("GOOGLE", "saved-key", "gemini-3.8-flash", capability_rules=[])
    assert provider is build.return_value and not isinstance(provider, FallbackLLMProvider)


def test_an_explicit_model_needs_a_verified_saved_config():
    from app.services.llm.base import LLMPermanentError
    with patch("app.services.llm.factory.AIProviderConfigRepository") as repo:
        repo.return_value.get_by_provider.return_value = SimpleNamespace(is_verified=False)
        with pytest.raises(LLMPermanentError):
            resolve_provider_model(MagicMock(), "GOOGLE", "gemini-3.8-flash")


def test_v2_scores_given_evidence_and_skips_the_confirmation_cache():
    from app.services.campaign.scoring_v2_service import ScoringV2Service
    service = ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=lambda: MagicMock())
    service.detection_repository = MagicMock()
    service.evidence_service = MagicMock()
    service.load_config = MagicMock(return_value={})
    record = SimpleNamespace(canonical_skill_id=uuid4(), sense=None, sources=["SCAN"], best_source="SCAN",
                             match_tier="SCAN", recognition=1.0, evidence=[{"text": "line"}])
    seen = {}

    class StopAfterConfirm(Exception):
        pass

    def fake_confirm(**kwargs):
        seen.update(kwargs)
        raise StopAfterConfirm

    with patch("app.services.campaign.scoring_v2_service.jd_core_skill_specs", return_value=[object()]), \
            patch("app.services.campaign.scoring_v2_service.CoreSkillConfirmationService") as confirmation:
        confirmation.return_value.confirm.side_effect = fake_confirm
        service.db.query.return_value.filter.return_value.all.return_value = []
        try:
            service.score(campaign=MagicMock(), campaign_candidate=MagicMock(), job_description=MagicMock(),
                          resume=MagicMock(), experience_result=None, education_result=None, domain_result=None,
                          acted_on=False, persist=False, evidence_records=[record], use_confirmation_cache=False)
        except Exception:
            pass

    service.detection_repository.list_evidence.assert_not_called()
    assert seen["evidence_rows"] == [record] and seen["use_cache"] is False and seen["persist"] is False


def test_comparison_cost_counts_thinking_as_output_and_meter_adds_up_calls():
    from scripts.extraction_model_comparison import PRICES, UsageMeter, _cost
    usage = {"input": 1_000_000, "output": 500_000, "thinking": 500_000}
    assert _cost("gemini-3.5-flash-lite", usage) == pytest.approx(0.30 + 2.50)
    assert _cost("gemini-3.8-flash", usage) == pytest.approx(0.75 + 3.75)
    assert _cost("gemini-3.8-flash", usage, PRICES["gemini-3.8-flash"]["from_2027"]) == pytest.approx(1.50 + 7.50)
    assert _cost("unknown-model", usage) is None

    inner = MagicMock(model="gemini-3.8-flash")
    inner.generate_json.return_value = {"ok": True}
    meter = UsageMeter(inner)
    for usage_ in ({"input": 10, "output": 2, "thinking": 1}, {"input": 5, "output": 1, "thinking": 0}):
        inner.last_usage = usage_
        meter.generate_json("p", object())
    assert meter.snapshot() == {"calls": 2, "input": 15, "output": 3, "thinking": 1}
    assert meter.model == "gemini-3.8-flash"


def test_a_dropped_gemini_connection_is_a_transient_unavailable_error():
    import httpx
    from app.services.llm.base import LLMErrorReason, LLMTransientError
    with patch("app.services.llm.gemini_provider.genai.Client") as client:
        client.return_value.models.generate_content.side_effect = httpx.RemoteProtocolError(
            "Server disconnected without sending a response.")
        provider = GeminiProvider("key", "gemini-3.8-flash")
    with pytest.raises(LLMTransientError) as raised:
        provider.generate_json("prompt", MagicMock())
    assert raised.value.reason == LLMErrorReason.UNAVAILABLE
