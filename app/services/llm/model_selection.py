"""
Which model each AI step uses - a registered provider can split its work
across models by purpose, to put a cheap model on high-volume extraction
and a stronger one on AI evaluation.

Each model assignment on a provider has a `usage`:
    EXTRACTION  - JD parsing + resume parsing
    EVALUATION  - AI evaluation
    BOTH        - either, when no model is set for that purpose specifically

A step picks the model assigned to its own purpose, else the BOTH one. Every
assignment may name a backup model (same provider, same API key) that takes
over when the main one is unavailable - see FallbackLLMProvider.

Pure functions only - shared by the Settings service (validation on save),
the provider factory (selection at call time) and tests.
"""
from dataclasses import dataclass


class LLMPurpose:
    EXTRACTION = "EXTRACTION"
    EVALUATION = "EVALUATION"


class ModelUsage:
    EXTRACTION = "EXTRACTION"
    EVALUATION = "EVALUATION"
    BOTH = "BOTH"

    ALL = (EXTRACTION, EVALUATION, BOTH)
    LABELS = {EXTRACTION: "Extraction", EVALUATION: "Evaluation", BOTH: "Both"}


MAX_MODEL_ASSIGNMENTS = 3


@dataclass(frozen=True)
class ModelAssignment:
    usage: str
    model_name: str
    fallback_model_name: str | None = None


class ModelAssignmentError(ValueError):
    """A model setup that can't be saved - the message is shown to the admin as-is."""


def _key(name: str | None) -> str:
    return (name or "").strip().lower()


def normalize_assignments(assignments) -> list[ModelAssignment]:
    """Strips names and turns a blank backup into None. Accepts ModelAssignment or any object/dict with the same fields."""
    normalized = []
    for a in assignments or []:
        get = a.get if isinstance(a, dict) else lambda k, a=a: getattr(a, k, None)
        usage = (get("usage") or "").strip().upper()
        model = (get("model_name") or "").strip()
        fallback = (get("fallback_model_name") or "").strip() or None
        normalized.append(ModelAssignment(usage=usage, model_name=model, fallback_model_name=fallback))
    return normalized


def validate_assignments(assignments) -> list[ModelAssignment]:
    """
    Returns the normalized assignments, or raises ModelAssignmentError with
    the first problem found. Model names compare case-insensitively.
    """
    items = normalize_assignments(assignments)
    if not items:
        raise ModelAssignmentError("Add at least one model.")
    if len(items) > MAX_MODEL_ASSIGNMENTS:
        raise ModelAssignmentError(f"A provider can have at most {MAX_MODEL_ASSIGNMENTS} models.")

    seen_usage: set[str] = set()
    seen_models: dict[str, str] = {}
    for item in items:
        if item.usage not in ModelUsage.ALL:
            raise ModelAssignmentError("Choose what each model is used for: Extraction, Evaluation or Both.")
        label = ModelUsage.LABELS[item.usage]
        if not item.model_name:
            raise ModelAssignmentError(f"Select a model for {label}.")
        if item.usage in seen_usage:
            raise ModelAssignmentError(f"Only one model can be used for {label}.")
        seen_usage.add(item.usage)

        if _key(item.model_name) in seen_models:
            raise ModelAssignmentError(
                f"{item.model_name} is already used for {seen_models[_key(item.model_name)]}. "
                "Use one model set to Both instead of adding it twice."
            )
        seen_models[_key(item.model_name)] = label

        if item.fallback_model_name and _key(item.fallback_model_name) == _key(item.model_name):
            raise ModelAssignmentError(f"The backup model for {label} must be different from its main model.")

    if ModelUsage.BOTH in seen_usage and {ModelUsage.EXTRACTION, ModelUsage.EVALUATION} <= seen_usage:
        raise ModelAssignmentError(
            "The Both model would never be used, because Extraction and Evaluation each have their own model. "
            "Remove one of the three."
        )
    for purpose in (ModelUsage.EXTRACTION, ModelUsage.EVALUATION):
        if purpose not in seen_usage and ModelUsage.BOTH not in seen_usage:
            raise ModelAssignmentError(
                f"Nothing handles {ModelUsage.LABELS[purpose]}. Add a model for it, or set a model to Both."
            )
    return items


def select_assignment(assignments, purpose: str, default_model: str | None = None) -> ModelAssignment | None:
    """The assignment a step with this purpose uses: its own, else BOTH, else the provider's single legacy model."""
    items = normalize_assignments(assignments)
    by_usage = {a.usage: a for a in items}
    chosen = by_usage.get(purpose) or by_usage.get(ModelUsage.BOTH)
    if chosen is not None:
        return chosen
    return ModelAssignment(usage=ModelUsage.BOTH, model_name=default_model) if default_model else None


def distinct_model_names(assignments) -> list[str]:
    """Every model (main + backup) a setup would call, each once, in first-seen order - what Save must verify."""
    names: list[str] = []
    seen: set[str] = set()
    for a in normalize_assignments(assignments):
        for name in (a.model_name, a.fallback_model_name):
            if name and _key(name) not in seen:
                seen.add(_key(name))
                names.append(name)
    return names
