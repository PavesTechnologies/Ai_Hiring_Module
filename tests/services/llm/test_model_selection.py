import pytest

from app.services.llm.model_selection import (
    LLMPurpose,
    ModelAssignment,
    ModelAssignmentError,
    distinct_model_names,
    select_assignment,
    validate_assignments,
)

"""
Per-purpose model setup rules: one model per purpose, no model twice, a
backup differs from its main model, and extraction + evaluation are both
covered.
"""


def _a(usage, model, fallback=None):
    return {"usage": usage, "model_name": model, "fallback_model_name": fallback}


@pytest.mark.parametrize("setup", [
    [_a("BOTH", "flash")],
    [_a("EXTRACTION", "flash-lite"), _a("EVALUATION", "pro")],
    [_a("BOTH", "flash"), _a("EVALUATION", "pro", "flash")],     # backup may be another row's main model
    [_a("EXTRACTION", "lite", "flash"), _a("EVALUATION", "pro", "flash")],  # same backup for both is fine
])
def test_valid_setups(setup):
    assert len(validate_assignments(setup)) == len(setup)


@pytest.mark.parametrize("setup,message", [
    ([], "Add at least one model."),
    ([_a("EXTRACTION", "a"), _a("EVALUATION", "b"), _a("BOTH", "c"), _a("BOTH", "d")], "at most 3"),
    ([_a("SUMMARY", "a")], "Extraction, Evaluation or Both"),
    ([_a("BOTH", "  ")], "Select a model for Both."),
    ([_a("EXTRACTION", "a"), _a("EXTRACTION", "b")], "Only one model can be used for Extraction."),
    ([_a("EXTRACTION", "flash"), _a("EVALUATION", "Flash")], "Flash is already used for Extraction"),
    ([_a("BOTH", "flash", "FLASH ")], "backup model for Both must be different"),
    ([_a("EXTRACTION", "a"), _a("EVALUATION", "b"), _a("BOTH", "c")], "would never be used"),
    ([_a("EXTRACTION", "a")], "Nothing handles Evaluation"),
    ([_a("EVALUATION", "a")], "Nothing handles Extraction"),
])
def test_invalid_setups_explain_the_problem(setup, message):
    with pytest.raises(ModelAssignmentError, match=message):
        validate_assignments(setup)


def test_values_are_trimmed_and_blank_backup_is_none():
    [item] = validate_assignments([_a(" both ", "  flash  ", "   ")])
    assert item == ModelAssignment("BOTH", "flash", None)


def test_selection_prefers_the_purpose_then_both():
    setup = [_a("BOTH", "flash"), _a("EVALUATION", "pro", "flash-2")]

    assert select_assignment(setup, LLMPurpose.EXTRACTION).model_name == "flash"
    evaluation = select_assignment(setup, LLMPurpose.EVALUATION)
    assert (evaluation.model_name, evaluation.fallback_model_name) == ("pro", "flash-2")


def test_selection_without_assignments_uses_legacy_model():
    assert select_assignment([], LLMPurpose.EVALUATION, default_model="old").model_name == "old"
    assert select_assignment([], LLMPurpose.EVALUATION) is None


def test_distinct_models_lists_main_and_backup_once():
    setup = [_a("EXTRACTION", "lite", "flash"), _a("EVALUATION", "pro", "Flash")]
    assert distinct_model_names(setup) == ["lite", "flash", "pro"]
