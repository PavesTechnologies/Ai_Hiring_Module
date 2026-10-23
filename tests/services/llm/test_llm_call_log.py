"""Every LLM request counts: CountedProvider records each one (tests record into a list - see conftest)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from app.services.llm import call_log
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError
from app.services.llm.call_log import CountedProvider, llm_call_context, llm_call_source, quota_day_start
from app.services.llm.fallback_provider import FallbackLLMProvider


class _Schema(BaseModel):
    ok: bool


class _Fake:
    provider_name = "GOOGLE"

    def __init__(self, model, replies):
        self.model, self.replies, self.last_usage = model, list(replies), None
        self.last_model_used = model

    def generate_json(self, prompt, response_schema, *, deterministic=False):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        self.last_usage = {"input": 100, "output": 20, "thinking": 5}
        return reply

    def list_models(self):
        return []


def test_every_request_is_recorded_with_its_label(_llm_calls_are_not_logged_to_the_database):
    rows = _llm_calls_are_not_logged_to_the_database
    provider = CountedProvider(_Fake("gemini-3.1-flash-lite", [{"ok": True}]), purpose="EXTRACTION")
    with llm_call_context("dry_run"), llm_call_source("skill_confirmation"):
        assert provider.generate_json("p", _Schema, deterministic=True) == {"ok": True}
    [row] = rows
    assert (row["provider"], row["model"], row["purpose"], row["source"], row["context"], row["outcome"]) == (
        "GOOGLE", "gemini-3.1-flash-lite", "EXTRACTION", "skill_confirmation", "dry_run", "ok")
    assert (row["input_tokens"], row["output_tokens"]) == (100, 25)             # thinking is billed as output
    assert provider.last_model_used == "gemini-3.1-flash-lite"                 # attributes pass through


def test_a_failed_request_counts_too_without_stale_tokens(_llm_calls_are_not_logged_to_the_database):
    rows = _llm_calls_are_not_logged_to_the_database
    inner = _Fake("gemini-3.1-flash-lite", [{"ok": True}, LLMPermanentError("quota", reason=LLMErrorReason.DAILY_LIMIT)])
    provider = CountedProvider(inner)
    provider.generate_json("p", _Schema)
    with pytest.raises(LLMPermanentError):
        provider.generate_json("p", _Schema)
    assert [r["outcome"] for r in rows] == ["ok", "DAILY_LIMIT"]
    assert rows[1]["input_tokens"] is None and rows[1]["context"] == "app"


def test_main_and_backup_model_each_count(_llm_calls_are_not_logged_to_the_database):
    rows = _llm_calls_are_not_logged_to_the_database
    main = CountedProvider(_Fake("gemini-3.1-flash-lite", [LLMTransientError("busy", reason=LLMErrorReason.UNAVAILABLE)]))
    backup = CountedProvider(_Fake("gemini-3.5-flash-lite", [{"ok": True}]))
    FallbackLLMProvider(main, backup, None).generate_json("p", _Schema)
    assert [(r["model"], r["outcome"]) for r in rows] == [
        ("gemini-3.1-flash-lite", "UNAVAILABLE"), ("gemini-3.5-flash-lite", "ok")]


def test_a_log_write_failure_never_fails_the_call():
    call_log.set_recorder(MagicMock(side_effect=RuntimeError("db down")))
    assert CountedProvider(_Fake("m", [{"ok": True}])).generate_json("p", _Schema) == {"ok": True}


def test_the_factory_wraps_every_provider_it_builds():
    from app.services.llm.factory import build_env_fallback_provider, build_provider
    with patch("app.services.llm.factory.GeminiProvider") as gemini:
        gemini.return_value = SimpleNamespace(model="gemini-3.1-flash-lite", provider_name="GOOGLE")
        built = build_provider("GOOGLE", "key", "gemini-3.1-flash-lite", purpose="EVALUATION")
        env = build_env_fallback_provider("EXTRACTION")
    assert isinstance(built, CountedProvider) and built._purpose == "EVALUATION"
    assert isinstance(env, CountedProvider) and env._purpose == "EXTRACTION"


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 23, 5, 38, tzinfo=timezone.utc), datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)),   # PDT
    (datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc), datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)),
    (datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc), datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)),      # PST
])
def test_the_usage_day_is_the_providers_quota_day_midnight_pacific(now, expected):
    assert quota_day_start(now) == expected


@pytest.mark.parametrize("raw, expected", [
    ("400", {"default": 400}),                                                       # a plain number: every model
    ('{"default": 400, "gemini-3.1-flash-lite": 400, "gemini-3.5-flash-lite": 300}',
     {"default": 400, "gemini-3.1-flash-lite": 400, "gemini-3.5-flash-lite": 300}),
    ('{"gemini-3.1-flash-lite": 400}', {"gemini-3.1-flash-lite": 400, "default": 300}),  # default when missing
    ("not json", {"default": 300}),
    (None, {"default": 300}),
])
def test_the_daily_cap_is_per_model(raw, expected):
    from app.services.campaign.scoring_v2_service import daily_llm_call_caps
    repo = MagicMock()
    repo.get_configs_by_keys.return_value = {"SCORING_V2_DAILY_LLM_CALL_CAP": raw} if raw is not None else {}
    assert daily_llm_call_caps(repo) == expected


def _budget_service(used_by_model, caps, provider):
    from app.services.campaign import scoring_v2_service as svc
    service = svc.ScoringV2Service.__new__(svc.ScoringV2Service)
    service.db, service.config_repository, service.provider_resolver = MagicMock(), MagicMock(), lambda: provider
    return svc, service, patch.object(svc, "daily_llm_call_caps", return_value=caps), patch.object(
        svc, "calls_in_quota_day", side_effect=lambda db, model=None: used_by_model.get(model, 0))


def test_the_v2_budget_passes_while_the_main_or_the_backup_model_has_room():
    caps = {"default": 400, "gemini-3.1-flash-lite": 400, "gemini-3.5-flash-lite": 400}
    provider = FallbackLLMProvider(_Fake("gemini-3.1-flash-lite", []), _Fake("gemini-3.5-flash-lite", []), None)
    svc, service, p1, p2 = _budget_service({"gemini-3.1-flash-lite": 399, "gemini-3.5-flash-lite": 10}, caps, provider)
    with p1, p2:
        assert service._budget(3) is True                      # the main model is full, the backup has room
    svc, service, p1, p2 = _budget_service({"gemini-3.1-flash-lite": 399, "gemini-3.5-flash-lite": 398}, caps, provider)
    with p1, p2:
        assert service._budget(3) is False and service._budget(1) is True


def test_a_model_without_its_own_cap_uses_the_default():
    single = CountedProvider(_Fake("gemini-9-pro", []))
    svc, service, p1, p2 = _budget_service({"gemini-9-pro": 98}, {"default": 100}, single)
    with p1, p2:
        assert service._budget(2) is True and service._budget(3) is False
