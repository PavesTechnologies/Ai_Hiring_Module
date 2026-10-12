from unittest.mock import MagicMock

import pytest

from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError
from app.services.llm.fallback_provider import FallbackLLMProvider

"""
Main -> backup failover: only failures a different model can get around
switch models; a failed main model is skipped for a while via Redis.
"""


def _provider(model, result=None, error=None):
    provider = MagicMock()
    provider.provider_name, provider.model = "GOOGLE", model
    if error is not None:
        provider.generate_json.side_effect = error
    else:
        provider.generate_json.return_value = result or {"ok": model}
    return provider


def _cache():
    store = {}
    cache = MagicMock()
    cache.get.side_effect = store.get
    cache.set.side_effect = lambda key, value, ttl=None: store.__setitem__(key, (value, ttl))
    return cache, store


def _error(reason, cls=LLMPermanentError, message="boom"):
    return cls(message, reason=reason)


def test_main_model_answers_when_healthy():
    llm = FallbackLLMProvider(_provider("main"), _provider("backup"), _cache()[0])

    assert llm.generate_json("p", dict) == {"ok": "main"}
    assert (llm.last_model_used, llm.last_used_fallback) == ("main", False)
    llm.fallback.generate_json.assert_not_called()


def test_daily_limit_on_main_uses_backup_and_marks_main_down():
    cache, store = _cache()
    llm = FallbackLLMProvider(_provider("main", error=_error(LLMErrorReason.DAILY_LIMIT)), _provider("backup"), cache)

    assert llm.generate_json("p", dict) == {"ok": "backup"}
    assert (llm.last_model_used, llm.last_used_fallback) == ("backup", True)
    [(reason, ttl)] = store.values()
    assert reason == LLMErrorReason.DAILY_LIMIT and ttl == 1800


def test_marked_main_is_skipped():
    main = _provider("main")
    cache, _ = _cache()
    cache.get.side_effect = lambda key: "x" if key.endswith(":GOOGLE:main") else None
    llm = FallbackLLMProvider(main, _provider("backup"), cache)

    assert llm.generate_json("p", dict) == {"ok": "backup"}
    main.generate_json.assert_not_called()


def test_marked_main_is_retried_when_backup_also_fails():
    cache, _ = _cache()
    cache.get.side_effect = lambda key: "x" if key.endswith(":GOOGLE:main") else None
    llm = FallbackLLMProvider(
        _provider("main"), _provider("backup", error=_error(LLMErrorReason.UNAVAILABLE, LLMTransientError)), cache,
    )

    assert llm.generate_json("p", dict) == {"ok": "main"}
    assert llm.last_used_fallback is False


@pytest.mark.parametrize("reason", [
    LLMErrorReason.INVALID_KEY, LLMErrorReason.CREDITS_EXHAUSTED, LLMErrorReason.BAD_REQUEST, LLMErrorReason.REFUSED,
])
def test_failures_the_backup_would_share_are_raised_without_fallback(reason):
    backup = _provider("backup")
    llm = FallbackLLMProvider(_provider("main", error=_error(reason)), backup, _cache()[0])

    with pytest.raises(LLMPermanentError):
        llm.generate_json("p", dict)
    backup.generate_json.assert_not_called()


def test_rate_limit_skip_follows_retry_hint():
    cache, store = _cache()
    error = _error(LLMErrorReason.RATE_LIMITED, LLMTransientError, "429. Please retry in 12.5s.")
    FallbackLLMProvider(_provider("main", error=error), _provider("backup"), cache).generate_json("p", dict)

    [(_, ttl)] = store.values()
    assert ttl == 13


def test_bad_output_uses_backup_without_marking_main_down():
    cache, store = _cache()
    llm = FallbackLLMProvider(
        _provider("main", error=_error(LLMErrorReason.BAD_OUTPUT)), _provider("backup"), cache,
    )

    assert llm.generate_json("p", dict) == {"ok": "backup"}
    assert store == {}


def test_both_failing_raises_the_backup_error_chained_to_main():
    main_error = _error(LLMErrorReason.DAILY_LIMIT)
    backup_error = _error(LLMErrorReason.DAILY_LIMIT, message="backup also out")
    llm = FallbackLLMProvider(_provider("main", error=main_error), _provider("backup", error=backup_error), None)

    with pytest.raises(LLMPermanentError) as exc_info:
        llm.generate_json("p", dict)
    assert exc_info.value is backup_error
    assert exc_info.value.__cause__ is main_error


def test_works_without_redis():
    llm = FallbackLLMProvider(_provider("main", error=_error(LLMErrorReason.DAILY_LIMIT)), _provider("backup"), None)
    assert llm.generate_json("p", dict) == {"ok": "backup"}
