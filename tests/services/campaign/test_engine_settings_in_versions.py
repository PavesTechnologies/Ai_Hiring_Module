"""Settings phase 3: the four engine settings live in the scoring version's configuration, validated."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.scoring_config_schema import SCHEMA, configuration_problems
from app.services.campaign.scoring_v2 import DEFAULT_CONFIG, merge_config
from app.services.campaign.scoring_versions import validate_configuration
from app.services.campaign.validation_inputs import validation_settings


def test_the_defaults_are_the_values_platform_config_held():
    """Moving them changed nothing: 0.0 years tolerance, 8.0 years for a degree, score doesn't reject, one entry."""
    assert DEFAULT_CONFIG["validation"] == {"experience_tolerance_years": 0.0, "equivalent_experience_years": 8.0}
    assert DEFAULT_CONFIG["score_threshold_rejects"] is False
    assert [(e["term"], e["skill"]) for e in DEFAULT_CONFIG["match_blocklist"]] == [("IT governance", "AI Governance")]


def test_validation_settings_come_from_the_given_version_or_the_current_one():
    version_cfg = merge_config({"validation": {"experience_tolerance_years": 1.5}})
    assert validation_settings(None, version_cfg)["experience_tolerance_years"] == 1.5
    assert validation_settings(None, version_cfg)["equivalent_experience_years"] == 8.0      # default kept
    current = SimpleNamespace(configuration={"validation": {"equivalent_experience_years": None}})
    with patch("app.services.campaign.scoring_versions.current_version", return_value=current):
        assert validation_settings(MagicMock())["equivalent_experience_years"] is None


def test_a_candidate_versions_validation_settings_reach_its_shadow_scoring():
    from app.services.campaign.validation_inputs import build_validation_inputs
    jd = SimpleNamespace(min_experience_years=5, education_criteria={}, extracted_json={}, calibration=None)
    resume = SimpleNamespace(parsed_json={})
    captured = {}

    class Validation:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def validate_experience(self, *a, **k):
            return None

        def validate_education(self, *a, **k):
            return None

    with patch("app.services.campaign.validation_inputs.ExperienceEducationValidationService", Validation), \
            patch("app.services.campaign.validation_inputs.field_families_for_jd", return_value=[]), \
            patch("app.services.campaign.validation_inputs.apply_experience_tolerance", lambda r, jd: r), \
            patch("app.services.campaign.validation_inputs.apply_education_requirement", lambda r, jd: r), \
            patch("app.services.campaign.validation_inputs.experience_minimum_inputs", lambda jd, m: (m, None)), \
            patch("app.services.campaign.validation_inputs.jd_internship_credit", return_value=100), \
            patch("app.services.campaign.validation_inputs.DomainCapabilityMatchingService"):
        build_validation_inputs(MagicMock(), jd, resume,
                                config=merge_config({"validation": {"experience_tolerance_years": 2.0}}))
    assert captured == {"experience_tolerance_years": 2.0, "equivalent_experience_years": 8.0}


def test_the_schema_covers_every_section_but_the_shared_evidence():
    assert set(SCHEMA) == set(DEFAULT_CONFIG) - {"evidence"}
    assert configuration_problems({k: v for k, v in DEFAULT_CONFIG.items() if k != "evidence"}) == []


@pytest.mark.parametrize("configuration, problem", [
    ({"importance_weights": {"core": -1}}, "importance_weights.core: give a value from 0 to 10"),
    ({"importance_weights": {"cor": 2}}, "importance_weights.cor: unknown setting"),
    ({"hierarchy_multipliers": {"CHILD": 1.5}}, "from 0 to 1"),
    ({"preferred_share": "0.1"}, "preferred_share: give a number"),
    ({"paraphrase": {"max_lines": 2.5}}, "whole number"),
    ({"paraphrase": {"sections": ["EDUCATION"]}}, "paraphrase.sections"),
    ({"paraphrase": {"min_words": 50, "max_words": 10}}, "min_words can't be more than max_words"),
    ({"validation": {"equivalent_experience_years": 99}}, "from 0 to 40"),
    ({"score_threshold_rejects": "yes"}, "true or false"),
    ({"match_blocklist": [{"term": "x"}]}, "match_blocklist[0].skill"),
    ({"match_blocklist": "IT governance"}, "a list of"),
])
def test_invalid_values_are_refused_with_every_problem(configuration, problem):
    with pytest.raises(CampaignException) as exc:
        validate_configuration(configuration)
    assert any(problem in p for p in exc.value.data["problems"]), exc.value.data["problems"]


def test_a_valid_partial_configuration_is_accepted():
    assert validate_configuration({"validation": {"equivalent_experience_years": None},
                                   "score_threshold_rejects": True}) is not None


def test_a_version_summary_shows_every_setting_it_scores_with():
    from app.services.campaign.scoring_versions import version_summary
    version = SimpleNamespace(id="v", label="x", status="candidate", model=None, note=None, created_at=None,
                              activated_at=None, retired_at=None, configuration={"score_threshold_rejects": True})
    effective = version_summary(version)["effective_configuration"]
    assert effective["score_threshold_rejects"] is True and effective["validation"]["equivalent_experience_years"] == 8.0
    assert "evidence" not in effective
