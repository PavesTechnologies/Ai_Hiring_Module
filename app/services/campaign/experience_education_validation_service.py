"""
M07-E02: Experience & Education Validation.

Reads JobDescription.min_experience_years / education_criteria (already
persisted by the JD pipeline) against a candidate's parsed resume data
(Resume.parsed_json["total_experience_years"] / ["education"], already
persisted by the Resume pipeline - see ResumeExtractionResponse). Neither
side requires new columns; this service only interprets data that already
exists.

Design notes (informed by the M07-E01 hierarchy-matching precedent already
established in this codebase):

- Education is matched via a degree-LEVEL hierarchy, not an exact string or
  an explicit multi-degree list - a candidate holding ANY degree at or
  above the required level satisfies it (so "Bachelor's, Master's, or PhD
  all acceptable" falls out of the hierarchy for free, satisfying the
  "multiple acceptable degrees" requirement without changing the JD
  schema/API).
- The required level prefers the JD's AI-extracted
  JDExtractionResponse.education.degree_level (extracted_json["education"],
  see Education in app/schemas/ai/jd_extraction_response.py) over
  classifying JobDescription.education_criteria's recruiter free text -
  same "prefer the extraction pipeline's own classification, fall back to
  deterministic keyword classification only when absent/UNKNOWN" pattern
  EducationMatchingService.normalize_candidate_education_entry already
  uses resume-side. Falls back to education_criteria's free text for JDs
  with no AI extraction (manually created via the JD form) or created
  before this field existed. Same preference order applies per candidate
  education entry (entry["degree_level"] over entry["degree"] text).
- Both validations are graceful about absent data: if the JD imposes no
  requirement, the check is SKIPPED (never held against the candidate). If
  the JD does require something but the resume has no usable data for it,
  that's flagged as DATA_MISSING - also never auto-failed, since a resume
  parsing gap is not the candidate's fault, but it's surfaced distinctly so
  HR can review it manually (M07-E02 S03).
- Every result carries `applicable` (False for both SKIPPED and
  DATA_MISSING) so the caller can exclude it from a weighted blend and
  renormalize across whatever remains applicable.
"""
from app.enums.education import DEGREE_LEVEL_RANK, RELATED_EDUCATION_FIELDS, DegreeLevel, EducationField
from app.services.education.education_matching_service import (
    classify_degree_level,
    classify_field,
    detect_related_field_allowed,
)

_DEFAULT_EXPERIENCE_TOLERANCE_YEARS = 0.0
_DEFAULT_EQUIVALENT_EXPERIENCE_YEARS = 8.0


_FIELD_FULL = "FULL_MATCH"
_FIELD_RELATED = "RELATED_FIELD_MATCH"
_FIELD_MISMATCH = "MISMATCH"
_FIELD_NOT_REQUIRED = "NOT_REQUIRED"
_FIELD_UNKNOWN = "UNKNOWN"

# Picks the best candidate entry (higher is better) - NOT_REQUIRED ties FULL.
_FIELD_MATCH_QUALITY = {
    _FIELD_FULL: 3, _FIELD_NOT_REQUIRED: 3, _FIELD_RELATED: 2, _FIELD_UNKNOWN: 1, _FIELD_MISMATCH: 0,
}
# Multiplier on the degree-level score: a related field passes but scores a
# little lower than an exact one; a mismatch fails and halves the score.
_FIELD_SCORE_FACTOR = {
    _FIELD_FULL: 1.0, _FIELD_NOT_REQUIRED: 1.0, _FIELD_UNKNOWN: 1.0, _FIELD_RELATED: 0.9, _FIELD_MISMATCH: 0.5,
}


def _coerce_field(value) -> EducationField | None:
    if value is None:
        return None
    if isinstance(value, EducationField):
        return value
    try:
        return EducationField(value)
    except ValueError:
        return None


def _coerce_degree_level(value) -> DegreeLevel | None:
    if value is None:
        return None
    if isinstance(value, DegreeLevel):
        return value
    try:
        return DegreeLevel(value)
    except ValueError:
        return None


def _rank_level(level: DegreeLevel | None) -> tuple[int, str] | None:
    """
    (rank, name) - the shape the rest of this file's comparison/display
    logic already expects. None whenever the level isn't confidently
    ranked against the academic ladder (UNKNOWN, or PROFESSIONAL/OTHER -
    see DEGREE_LEVEL_RANK's own docstring for why those two are
    deliberately unranked) - same "absent" treatment the old keyword-only
    classifier gave an unrecognized degree string.
    """
    if level is None:
        return None
    rank = DEGREE_LEVEL_RANK.get(level)
    return (rank, level.value) if rank is not None else None


class ExperienceEducationValidationService:

    def __init__(
        self,
        experience_tolerance_years: float = _DEFAULT_EXPERIENCE_TOLERANCE_YEARS,
        equivalent_experience_years: float | None = _DEFAULT_EQUIVALENT_EXPERIENCE_YEARS,
    ):
        self.experience_tolerance_years = experience_tolerance_years
        self.equivalent_experience_years = equivalent_experience_years

    # ------------------------------------------------------------------
    # S01: Experience Validation
    # ------------------------------------------------------------------

    def validate_experience(
        self,
        min_experience_years: float | None,
        candidate_total_years: float | None,
        jd_extracted_experience: dict | None = None,
    ) -> dict:
        # Same "JSON is authoritative" preference as education: the JD's
        # AI-extracted experience.min_experience_years (extracted_json,
        # JDExtractionResponse.experience) takes priority over the
        # separate, recruiter-typed JobDescription.min_experience_years
        # column whenever the JSON has a value - the two can drift apart
        # (e.g. a JD create/update form field never resynced after
        # re-extraction). Falls back to the DB column only for JDs with no
        # AI extraction at all (manually created) or no experience key yet.
        extracted_min = (jd_extracted_experience or {}).get("min_experience_years")
        if extracted_min is not None:
            min_experience_years = float(extracted_min)

        if min_experience_years is None:
            return self._experience_result(
                applicable=False, skipped=True, data_missing=False, passed=True, score=100.0,
                candidate_years=candidate_total_years, min_years=None, effective_min_years=None,
            )

        if candidate_total_years is None:
            return self._experience_result(
                applicable=False, skipped=False, data_missing=True, passed=True, score=None,
                candidate_years=None, min_years=min_experience_years, effective_min_years=None,
            )

        effective_min = max(min_experience_years - self.experience_tolerance_years, 0.0)
        passed = candidate_total_years >= effective_min
        score = 100.0 if passed else (
            round((candidate_total_years / effective_min) * 100, 2) if effective_min > 0 else 100.0
        )
        return self._experience_result(
            applicable=True, skipped=False, data_missing=False, passed=passed, score=score,
            candidate_years=candidate_total_years, min_years=min_experience_years, effective_min_years=effective_min,
        )

    @staticmethod
    def _experience_result(
        *, applicable, skipped, data_missing, passed, score, candidate_years, min_years, effective_min_years,
    ) -> dict:
        return {
            "applicable": applicable,
            "skipped": skipped,
            "data_missing": data_missing,
            "passed": passed,
            "score": score,
            "candidate_years": candidate_years,
            "min_years": min_years,
            "effective_min_years": effective_min_years,
        }

    # ------------------------------------------------------------------
    # S02: Education Validation
    # ------------------------------------------------------------------

    def validate_education(
        self,
        required_degree_text: str | None,
        candidate_education_entries: list[dict] | None,
        candidate_total_years: float | None,
        jd_extracted_education: dict | None = None,
        required_field_text: str | None = None,
    ) -> dict:
        """
        Degree LEVEL (hierarchy - any degree at or above the required level)
        AND field of study (exact, or a related field when the JD allows
        one - RELATED_EDUCATION_FIELDS). Either requirement may be absent;
        with neither, the check is SKIPPED. The candidate is judged on
        whichever education entry satisfies the most (e.g. a Master's-CS
        beats a Bachelor's-Mechanical for a Bachelor's-CS requirement).

        A candidate field that can't be classified is treated as missing
        data, never as a mismatch - same "a parsing gap is not the
        candidate's fault" rule as degree level.
        """
        # Raw display text - independent of whether a level could be
        # classified from it, and independent of skipped/data_missing below,
        # so the caller can always show the actual JD/resume-extracted
        # degree string instead of a "no qualifying degree" placeholder that
        # reads as "this candidate has no degree" when the real reason is
        # simply "the JD has no requirement" or "the level was unparseable".
        jd_education = jd_extracted_education or {}
        required_degree_display = required_degree_text or jd_education.get("degree")
        required_field_display = jd_education.get("field") or required_field_text
        candidate_degree_display = self._best_candidate_degree_text(candidate_education_entries)

        required_level = self._required_level(required_degree_text, jd_extracted_education)
        required_field = self._required_field(required_field_text, jd_extracted_education)
        related_field_allowed = bool(jd_education.get("related_field_allowed")) or detect_related_field_allowed(
            required_field_display,
        )
        field_context = {
            "required_field": required_field.value if required_field else None,
            "related_field_allowed": related_field_allowed,
            "required_field_text": required_field_display,
        }

        if required_level is None and required_field is None:
            return self._education_result(
                applicable=False, skipped=True, data_missing=False, passed=True, score=100.0,
                required_level=None, candidate_level=None, equivalent_experience_applied=False,
                required_degree_text=required_degree_display, candidate_degree_text=candidate_degree_display,
                field_match=_FIELD_NOT_REQUIRED, **field_context,
            )

        # (level, field, entry) - only entries that can be judged on what the
        # JD actually requires: a ranked level when a level is required,
        # otherwise a classified field.
        judged = [
            (self._candidate_level(entry), self._candidate_field(entry), entry)
            for entry in (candidate_education_entries or [])
        ]
        usable = [c for c in judged if (c[0] is not None if required_level is not None else c[1] is not None)]

        if not usable:
            equivalent = self._meets_equivalent_experience(candidate_total_years)
            return self._education_result(
                applicable=equivalent, skipped=False, data_missing=True, passed=True,
                score=100.0 if equivalent else None,
                required_level=required_level, candidate_level=None, equivalent_experience_applied=equivalent,
                required_degree_text=required_degree_display, candidate_degree_text=candidate_degree_display,
                field_match=_FIELD_UNKNOWN, **field_context,
            )

        def _assess(candidate):
            level, field, _ = candidate
            level_passed = required_level is None or level[0] >= required_level[0]
            return level_passed, self._field_match(required_field, field, related_field_allowed)

        def _quality(candidate):
            level_passed, match = _assess(candidate)
            return (level_passed, _FIELD_MATCH_QUALITY[match], candidate[0][0] if candidate[0] else 0)

        best = max(usable, key=_quality)
        best_level, best_field, best_entry = best
        level_passed, field_match = _assess(best)
        field_passed = field_match != _FIELD_MISMATCH
        passed = level_passed and field_passed

        equivalent_experience_applied = False
        if not passed and self._meets_equivalent_experience(candidate_total_years):
            passed = True
            equivalent_experience_applied = True

        if equivalent_experience_applied:
            score = 100.0
        else:
            level_score = 100.0 if level_passed else (best_level[0] / required_level[0]) * 100
            score = round(level_score * _FIELD_SCORE_FACTOR[field_match], 2)

        return self._education_result(
            applicable=True, skipped=False, data_missing=False, passed=passed, score=score,
            required_level=required_level, candidate_level=best_level,
            equivalent_experience_applied=equivalent_experience_applied,
            required_degree_text=required_degree_display,
            # The entry that actually decided pass/fail, not just "best" by
            # display preference - keeps the shown text consistent with the
            # level actually compared against the requirement.
            candidate_degree_text=(best_entry or {}).get("degree") or candidate_degree_display,
            field_match=field_match,
            candidate_field=best_field.value if best_field else None,
            candidate_field_text=(best_entry or {}).get("field"),
            level_passed=level_passed,
            field_passed=field_passed,
            **field_context,
        )

    def _best_candidate_degree_text(self, candidate_education_entries: list[dict] | None) -> str | None:
        """
        Display-only raw degree text for whichever entry ranks highest -
        computed independently of whether the JD has a requirement at all,
        so a candidate's real degree is never hidden just because nothing
        was asked of it (NOT_REQUIRED must not look like NO_DEGREE).
        Falls back to the first entry's raw text if none carry a
        confidently-classified level (e.g. an unparseable degree string).
        """
        entries = candidate_education_entries or []
        ranked = [
            (level, entry) for level, entry in (
                (self._candidate_level(entry), entry) for entry in entries
            )
            if level is not None
        ]
        if ranked:
            _, best_entry = max(ranked, key=lambda pair: pair[0][0])
            return best_entry.get("degree")
        return entries[0].get("degree") if entries else None

    def _meets_equivalent_experience(self, candidate_total_years: float | None) -> bool:
        return (
            self.equivalent_experience_years is not None
            and candidate_total_years is not None
            and candidate_total_years >= self.equivalent_experience_years
        )

    @staticmethod
    def _required_level(
        required_degree_text: str | None, jd_extracted_education: dict | None,
    ) -> tuple[int, str] | None:
        extracted = _coerce_degree_level((jd_extracted_education or {}).get("degree_level"))
        level = extracted if extracted is not None and extracted != DegreeLevel.UNKNOWN else (
            classify_degree_level(required_degree_text)
        )
        return _rank_level(level)

    @staticmethod
    def _candidate_level(entry: dict | None) -> tuple[int, str] | None:
        entry = entry or {}
        extracted = _coerce_degree_level(entry.get("degree_level"))
        level = extracted if extracted is not None and extracted != DegreeLevel.UNKNOWN else (
            classify_degree_level(entry.get("degree"))
        )
        return _rank_level(level)

    @staticmethod
    def _required_field(required_field_text: str | None, jd_extracted_education: dict | None) -> EducationField | None:
        """Same preference as _required_level: the JD's AI field_normalized, else the recruiter's field text."""
        extracted = _coerce_field((jd_extracted_education or {}).get("field_normalized"))
        field = extracted if extracted not in (None, EducationField.UNKNOWN) else classify_field(required_field_text)
        # OTHER can't be compared against anything - no requirement to check.
        return field if field not in (EducationField.UNKNOWN, EducationField.OTHER) else None

    @staticmethod
    def _candidate_field(entry: dict | None) -> EducationField | None:
        entry = entry or {}
        extracted = _coerce_field(entry.get("field_normalized"))
        field = extracted if extracted not in (None, EducationField.UNKNOWN) else classify_field(entry.get("field"))
        # OTHER stays - it's a known field that simply isn't the required one.
        return field if field != EducationField.UNKNOWN else None

    @staticmethod
    def _field_match(
        required_field: EducationField | None, candidate_field: EducationField | None, related_field_allowed: bool,
    ) -> str:
        if required_field is None:
            return _FIELD_NOT_REQUIRED
        if candidate_field is None:
            return _FIELD_UNKNOWN
        if candidate_field == required_field:
            return _FIELD_FULL
        if related_field_allowed and candidate_field in RELATED_EDUCATION_FIELDS.get(required_field, frozenset()):
            return _FIELD_RELATED
        return _FIELD_MISMATCH

    @staticmethod
    def _education_result(
        *, applicable, skipped, data_missing, passed, score, required_level, candidate_level,
        equivalent_experience_applied, required_degree_text=None, candidate_degree_text=None,
        field_match=None, required_field=None, candidate_field=None, related_field_allowed=False,
        required_field_text=None, candidate_field_text=None, level_passed=None, field_passed=None,
    ) -> dict:
        return {
            "applicable": applicable,
            "skipped": skipped,
            "data_missing": data_missing,
            "passed": passed,
            "score": score,
            "required_level": required_level[1] if required_level else None,
            "candidate_level": candidate_level[1] if candidate_level else None,
            "equivalent_experience_applied": equivalent_experience_applied,
            # Raw JD/resume-extracted degree text (see validate_education's
            # required_degree_display/candidate_degree_display) - always
            # populated when the underlying JSON has it, regardless of
            # skipped/data_missing/applicable.
            "required_degree_text": required_degree_text,
            "candidate_degree_text": candidate_degree_text,
            # Field of study - field_match is FULL_MATCH / RELATED_FIELD_MATCH /
            # MISMATCH / NOT_REQUIRED / UNKNOWN. level_passed/field_passed say
            # which half failed (both None when nothing was compared).
            "field_match": field_match,
            "required_field": required_field,
            "candidate_field": candidate_field,
            "related_field_allowed": related_field_allowed,
            "required_field_text": required_field_text,
            "candidate_field_text": candidate_field_text,
            "level_passed": level_passed,
            "field_passed": field_passed,
        }
