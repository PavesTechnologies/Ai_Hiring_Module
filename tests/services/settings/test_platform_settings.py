"""Settings phase 1: registered platform settings - validated, previewed, saved with a numbers-only audit."""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.settings.platform_settings import BY_KEY, REGISTRY, InvalidSetting, validate
from app.services.settings.platform_settings_service import PlatformSettingsService

APP = Path(__file__).resolve().parents[3] / "app"


class FakeConfigRepo:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.upserts, self.committed, self.rolled_back = [], False, False

    def get_configs_by_keys(self, keys):
        return {k: v for k, v in self.stored.items() if k in keys}

    def upsert_configs(self, updates, updated_by, descriptions):
        self.upserts.append((dict(updates), updated_by, dict(descriptions)))
        self.stored.update(updates)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _service(stored=None):
    repo, audit = FakeConfigRepo(stored), MagicMock()
    return PlatformSettingsService(repo, audit), repo, audit


def test_every_registered_setting_is_read_by_the_code():
    """A setting HR can edit that nothing reads would change nothing."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in APP.rglob("*.py")
                       if "seeds" not in p.parts and p.name != "platform_settings.py")
    unread = [s.key for s in REGISTRY if not re.search(r"\b" + s.key + r"\b", source)]
    assert unread == []


def test_defaults_sit_inside_their_ranges():
    for s in REGISTRY:
        assert s.minimum <= s.default <= s.maximum, s.key


def test_scoring_llm_and_consent_keys_are_not_editable_here():
    for key in ("EXPERIENCE_TOLERANCE_YEARS", "EQUIVALENT_EXPERIENCE_YEARS", "DETERMINISTIC_SCORE_REJECTS",
                "SCORING_V2_MATCH_BLOCKLIST", "CAMPAIGN_WEIGHT_AI", "SCORING_V2_DAILY_LLM_CALL_CAP",
                "JD_EMBEDDING_MAX_CHARS", "LLM_MODEL_CAPABILITIES", "CONSENT_VERSION", "JURISDICTION_CONSENT_CONFIG",
                "DEADLINE_CHECK_INTERVAL_HOURS"):
        assert key not in BY_KEY


@pytest.mark.parametrize("values, problem", [
    ({"NOT_A_SETTING": 1}, "can't be changed here"),
    ({"SCREENING_SLA_HOURS": "48"}, "Enter a number"),
    ({"SCREENING_SLA_HOURS": True}, "Enter a number"),
    ({"MAX_FILES_PER_ZIP": 2.5}, "whole number"),
    ({"MAX_EMAIL_RETRY_COUNT": 0}, "from 1 to 10"),
    ({"FAIRNESS_MIN_GROUP_SIZE": 1}, "from 2 to 1000"),
])
def test_invalid_values_are_refused_before_anything_is_saved(values, problem):
    service, repo, audit = _service()
    with pytest.raises(InvalidSetting) as exc:
        service.save(values, actor_id="hr-1")
    assert problem in str(exc.value)
    assert repo.upserts == [] and not audit.log.called


def test_every_problem_is_reported_at_once():
    with pytest.raises(InvalidSetting) as exc:
        validate({"MAX_FILES_PER_ZIP": 0, "NOPE": 1})
    assert set(exc.value.problems) == {"MAX_FILES_PER_ZIP", "NOPE"}


def test_list_shows_stored_values_and_marks_defaults():
    service, _, _ = _service({"SCREENING_SLA_HOURS": "36", "DEADLINE_WARNING_DAYS": "junk"})
    items = {i["key"]: i for g in service.list() for i in g["settings"]}
    assert (items["SCREENING_SLA_HOURS"]["value"], items["SCREENING_SLA_HOURS"]["is_default"]) == (36, False)
    assert (items["DEADLINE_WARNING_DAYS"]["value"], items["DEADLINE_WARNING_DAYS"]["is_default"]) == (3, True)
    assert (items["HM_REVIEW_SLA_DAYS"]["value"], items["HM_REVIEW_SLA_DAYS"]["is_default"]) == (5, True)


def test_preview_shows_before_after_and_warnings_without_saving():
    service, repo, audit = _service({"SCREENING_SLA_HOURS": "48", "FAIRNESS_MIN_GROUP_SIZE": "10"})
    result = service.preview({"SCREENING_SLA_HOURS": 24, "FAIRNESS_MIN_GROUP_SIZE": 3, "DEADLINE_WARNING_DAYS": 3})
    assert result["changes"] == [
        {"key": "SCREENING_SLA_HOURS", "label": "Screening SLA", "unit": "hours", "before": 48, "after": 24.0},
        {"key": "FAIRNESS_MIN_GROUP_SIZE", "label": "Fairness report: smallest group", "unit": "candidates",
         "before": 10, "after": 3}]
    assert [w["key"] for w in result["warnings"]] == ["FAIRNESS_MIN_GROUP_SIZE"]
    assert repo.upserts == [] and not audit.log.called


def test_save_writes_only_changes_and_audits_numbers():
    service, repo, audit = _service({"SCREENING_SLA_HOURS": "48"})
    result = service.save({"SCREENING_SLA_HOURS": 24, "HM_REVIEW_SLA_DAYS": 4, "DEADLINE_WARNING_DAYS": 3},
                          actor_id="hr-1")
    assert result["changed"] == ["HM_REVIEW_SLA_DAYS", "SCREENING_SLA_HOURS"]
    updates, actor, descriptions = repo.upserts[0]
    assert updates == {"SCREENING_SLA_HOURS": "24", "HM_REVIEW_SLA_DAYS": "4"} and actor == "hr-1"
    assert set(descriptions) == {"SCREENING_SLA_HOURS", "HM_REVIEW_SLA_DAYS"}     # a never-seeded key gets one
    details = audit.log.call_args.kwargs["details"]
    assert details["changes"] == {"SCREENING_SLA_HOURS": {"before": 48, "after": 24.0},
                                  "HM_REVIEW_SLA_DAYS": {"before": 5, "after": 4}}
    assert repo.committed


def test_saving_the_same_values_changes_and_audits_nothing():
    service, repo, audit = _service({"SCREENING_SLA_HOURS": "48"})
    assert service.save({"SCREENING_SLA_HOURS": 48}, actor_id="hr-1")["changed"] == []
    assert repo.upserts == [] and not audit.log.called


def test_a_failed_save_rolls_back():
    service, repo, audit = _service()
    audit.log.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        service.save({"SCREENING_SLA_HOURS": 24}, actor_id="hr-1")
    assert repo.rolled_back and not repo.committed


def test_the_embedding_retry_waits_must_not_be_reversed():
    """Settings phase 4: checked on the values as they would be after saving."""
    service, repo, audit = _service({"EMBED_RETRY_BASE_DELAY_SECONDS": "30"})
    with pytest.raises(InvalidSetting) as exc:
        service.save({"EMBED_RETRY_MAX_DELAY_SECONDS": 20}, actor_id="hr-1")
    assert "EMBED_RETRY_MAX_DELAY_SECONDS" in exc.value.problems and repo.upserts == []
    assert service.preview({"EMBED_RETRY_MAX_DELAY_SECONDS": 60})["changes"][0]["after"] == 60

