"""
Deterministic education matching (Parts 10-16).

Architecture note (found during investigation, worth keeping visible here):
`JobDescription.education_criteria` - the field
`ExperienceEducationValidationService.validate_education` actually reads and
the field this module's JD-side normalization reads too - is recruiter-typed
free text (`{"degree": ..., "field": ...}`, captured via the JD create/update
form), NOT the AI-extracted `JDExtractionResponse.education` object. The two
are separate, parallel structures; only `education_criteria` drives scoring.
Because of that, JD-side degree/field normalization here is deterministic
keyword classification, not an AI call - which also keeps this module fully
compliant with Part 15 ("the final JD-vs-resume education decision must
remain deterministic... do not call an LLM during candidate scoring").

Resume-side entries, by contrast, ARE produced by the AI resume-extraction
pipeline (EducationEntry.degree_level/field_normalized, Part 10-12) - this
module prefers those AI-classified values when present, and falls back to
its own deterministic classifier only when they're absent or UNKNOWN
(legacy resumes processed before this feature existed - Part 21).

This module is entirely new and additive: it does not modify
ExperienceEducationValidationService (Part 19) and is not wired into
deterministic_passed/the experience-education blend - Part 16 asks for the
correct qualification RESULT to be established first, as its own concern,
kept separate from the skill stage (Part 9).
"""
from app.enums.education import (
    DEGREE_LEVEL_RANK,
    RELATED_EDUCATION_FIELDS,
    DegreeLevel,
    EducationField,
    EducationMatchResult,
)

# Ordered CERTIFICATE -> DOCTORATE (rank order) so that when a JD/resume
# degree string matches keywords from more than one tier (e.g. a résumé
# listing "Bachelor's and Master's"), classify_degree_level keeps the
# highest-ranked match rather than the first one found.
_DEGREE_LEVEL_KEYWORDS = (
    (DegreeLevel.CERTIFICATE, ("certificate",)),
    (DegreeLevel.DIPLOMA, ("diploma",)),
    (DegreeLevel.ASSOCIATE, ("associate",)),
    (DegreeLevel.BACHELOR, (
        "bachelor", "b.tech", "btech", "b.e.", " be ", "b.sc", "bsc",
        "bca", "b.a.", " ba ", "b.com", "bcom", "undergraduate",
    )),
    (DegreeLevel.POSTGRADUATE_DIPLOMA, (
        "postgraduate diploma", "post graduate diploma", "pg diploma", "pgdm", "graduate diploma",
    )),
    (DegreeLevel.MASTER, (
        "master", "m.tech", "mtech", "m.e.", " me ", "m.sc", "msc",
        "mca", "mba", "m.a.", " ma ", "m.com", "mcom",
    )),
    (DegreeLevel.DOCTORATE, ("phd", "ph.d", "doctorate", "doctor of philosophy", "d.phil")),
)

# Checked only if nothing above matched - a professional certification
# (PMP, CPA, CFA, ...) is not part of the academic ladder at all.
_PROFESSIONAL_KEYWORDS = ("pmp", "cpa", "cfa", "professional certification", "chartered")

_FIELD_KEYWORDS = (
    (EducationField.COMPUTER_SCIENCE, ("computer science", "computer engineering", "cse")),
    (EducationField.INFORMATION_TECHNOLOGY, ("information technology", "information systems", " it ")),
    (EducationField.SOFTWARE_ENGINEERING, ("software engineering",)),
    (EducationField.DATA_SCIENCE, ("data science", "data analytics")),
    (EducationField.ELECTRONICS_ENGINEERING, ("electronics and communication", "electronics", "ece")),
    (EducationField.ELECTRICAL_ENGINEERING, ("electrical engineering", "electrical and electronics", "eee")),
    (EducationField.MECHANICAL_ENGINEERING, ("mechanical engineering", "mechanical")),
    (EducationField.CIVIL_ENGINEERING, ("civil engineering", "civil")),
    (EducationField.MATHEMATICS, ("mathematics", "applied math")),
    (EducationField.STATISTICS, ("statistics", "biostatistics")),
    (EducationField.BUSINESS_ADMINISTRATION, ("business administration", "management studies")),
    (EducationField.COMMERCE, ("commerce", "b.com", "m.com")),
    (EducationField.ECONOMICS, ("economics",)),
)

_RELATED_FIELD_PHRASES = ("or related", "or equivalent", "or similar", "related field", "related discipline")


def classify_degree_level(text: str | None) -> DegreeLevel:
    """
    Deterministic keyword classification into the Part 11 controlled
    vocabulary. Never guesses: returns UNKNOWN (not a best-effort tier) for
    unparseable/absent/unrecognized text - matching Part 11's "if it cannot
    confidently determine the level, degree_level = UNKNOWN. Do not guess."
    """
    if not text:
        return DegreeLevel.UNKNOWN
    lowered = f" {text.lower()} "

    best_rank = -1
    best_level = None
    for level, keywords in _DEGREE_LEVEL_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            rank = DEGREE_LEVEL_RANK[level]
            if rank > best_rank:
                best_rank = rank
                best_level = level
    if best_level is not None:
        return best_level

    if any(keyword in lowered for keyword in _PROFESSIONAL_KEYWORDS):
        return DegreeLevel.PROFESSIONAL

    return DegreeLevel.UNKNOWN


def classify_field(text: str | None) -> EducationField:
    """Deterministic keyword classification into the Part 12 controlled vocabulary. Never invents a field - UNKNOWN if unrecognized."""
    if not text:
        return EducationField.UNKNOWN
    lowered = text.lower()
    for field, keywords in _FIELD_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return field
    return EducationField.UNKNOWN


def detect_related_field_allowed(field_text: str | None) -> bool:
    """True only if the JD's field text explicitly allows a related/equivalent field (Part 13)."""
    if not field_text:
        return False
    lowered = field_text.lower()
    return any(phrase in lowered for phrase in _RELATED_FIELD_PHRASES)


def _coerce_enum(value, enum_cls):
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        return None


def normalize_jd_education_requirement(education_criteria: dict | None) -> dict:
    """
    education_criteria is recruiter free text ({"degree": ..., "field": ...}
    - see module docstring). Classified deterministically here - never AI,
    since this runs at candidate-match time, potentially long after the JD
    was created.
    """
    criteria = education_criteria or {}
    degree_text = criteria.get("degree")
    field_text = criteria.get("field")
    return {
        "degree_level": classify_degree_level(degree_text),
        "field_normalized": classify_field(field_text),
        "related_field_allowed": detect_related_field_allowed(field_text),
    }


def normalize_candidate_education_entry(entry: dict | None) -> dict:
    """
    Prefers the resume AI-extraction pipeline's own degree_level/
    field_normalized (Part 10-12) when present and not UNKNOWN; falls back
    to this module's deterministic classifier applied to the raw degree/
    field text otherwise - covers resumes processed before this feature
    existed (Part 21: no reprocessing required) without ever needing an
    LLM call here.
    """
    entry = entry or {}

    degree_level = _coerce_enum(entry.get("degree_level"), DegreeLevel)
    if degree_level is None or degree_level == DegreeLevel.UNKNOWN:
        degree_level = classify_degree_level(entry.get("degree"))

    field_normalized = _coerce_enum(entry.get("field_normalized"), EducationField)
    if field_normalized is None or field_normalized == EducationField.UNKNOWN:
        field_normalized = classify_field(entry.get("field"))

    return {"degree_level": degree_level, "field_normalized": field_normalized}


def match_education(
    required_degree_level: DegreeLevel,
    required_field: EducationField,
    related_field_allowed: bool,
    candidate_degree_level: DegreeLevel,
    candidate_field: EducationField,
) -> EducationMatchResult:
    """
    Part 14's deterministic comparison - degree level first (a hard gate:
    an insufficient level is DEGREE_LEVEL_MISMATCH regardless of field),
    then discipline/field, then whether a related field was explicitly
    allowed by the JD.
    """
    if required_degree_level == DegreeLevel.UNKNOWN:
        # No parseable/explicit JD requirement - nothing to confidently
        # match against. A caller that wants "no requirement -> always
        # satisfied" behavior (mirroring
        # ExperienceEducationValidationService's `skipped` semantics) must
        # special-case this itself; this function only reports what it can
        # actually determine.
        return EducationMatchResult.UNKNOWN

    required_rank = DEGREE_LEVEL_RANK.get(required_degree_level)
    candidate_rank = DEGREE_LEVEL_RANK.get(candidate_degree_level)

    if required_rank is None or candidate_rank is None:
        # PROFESSIONAL/OTHER/UNKNOWN on either side - no safe ordering
        # exists against the academic ladder (see DEGREE_LEVEL_RANK's
        # docstring), so this can only be a partial signal, never a
        # confident EXCEEDS/MISMATCH.
        return EducationMatchResult.PARTIAL_MATCH

    if candidate_rank < required_rank:
        return EducationMatchResult.DEGREE_LEVEL_MISMATCH

    field_matches = required_field == EducationField.UNKNOWN or candidate_field == required_field
    if field_matches:
        return (
            EducationMatchResult.FULL_MATCH
            if candidate_rank == required_rank
            else EducationMatchResult.DEGREE_LEVEL_EXCEEDS
        )

    field_related = (
        related_field_allowed
        and candidate_field in RELATED_EDUCATION_FIELDS.get(required_field, frozenset())
    )
    if field_related:
        return EducationMatchResult.RELATED_FIELD_MATCH

    return EducationMatchResult.DISCIPLINE_MISMATCH


# Used only to pick the best-matching candidate education entry when a
# candidate lists more than one (e.g. Bachelor's-Mechanical AND
# Master's-CS) - never surfaced outside this module, never converted into
# a numeric score (Part 16).
_RESULT_QUALITY_RANK = {
    EducationMatchResult.FULL_MATCH: 5,
    EducationMatchResult.DEGREE_LEVEL_EXCEEDS: 4,
    EducationMatchResult.RELATED_FIELD_MATCH: 3,
    EducationMatchResult.PARTIAL_MATCH: 2,
    EducationMatchResult.DEGREE_LEVEL_MISMATCH: 1,
    EducationMatchResult.DISCIPLINE_MISMATCH: 0,
}


def evaluate_candidate_education(
    education_criteria: dict | None,
    candidate_education_entries: list[dict] | None,
) -> dict:
    """
    Normalizes the JD's education_criteria and every candidate education
    entry, then reports whichever entry produces the best
    EducationMatchResult - a candidate with both a Bachelor's-Mechanical
    and a Master's-CS degree is judged on whichever one actually satisfies
    a Bachelor's-CS requirement, not on entry order.

    Purely deterministic (Part 15) - no LLM call happens here.
    """
    requirement = normalize_jd_education_requirement(education_criteria)
    required_degree_level = requirement["degree_level"]
    required_field = requirement["field_normalized"]
    related_field_allowed = requirement["related_field_allowed"]

    base_result = {
        "required_degree_level": required_degree_level.value,
        "required_field_normalized": required_field.value,
        "related_field_allowed": related_field_allowed,
        "candidate_degree_level": None,
        "candidate_field_normalized": None,
        "matched_entry_index": None,
    }

    if required_degree_level == DegreeLevel.UNKNOWN:
        return {**base_result, "result": EducationMatchResult.UNKNOWN.value}

    entries = candidate_education_entries or []
    if not entries:
        return {**base_result, "result": EducationMatchResult.NO_EDUCATION_DATA.value}

    best = None
    for index, entry in enumerate(entries):
        normalized = normalize_candidate_education_entry(entry)
        result = match_education(
            required_degree_level, required_field, related_field_allowed,
            normalized["degree_level"], normalized["field_normalized"],
        )
        if best is None or _RESULT_QUALITY_RANK[result] > _RESULT_QUALITY_RANK[best[0]]:
            best = (result, index, normalized)

    result, index, normalized = best
    return {
        **base_result,
        "result": result.value,
        "candidate_degree_level": normalized["degree_level"].value,
        "candidate_field_normalized": normalized["field_normalized"].value,
        "matched_entry_index": index,
    }
