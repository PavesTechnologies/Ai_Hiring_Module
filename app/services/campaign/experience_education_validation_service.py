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
from app.enums.education import DEGREE_LEVEL_RANK, DegreeLevel
from app.services.education.education_matching_service import classify_degree_level

_DEFAULT_EXPERIENCE_TOLERANCE_YEARS = 0.0
_DEFAULT_EQUIVALENT_EXPERIENCE_YEARS = 8.0


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
    ) -> dict:
        # Raw display text - independent of whether a level could be
        # classified from it, and independent of skipped/data_missing below,
        # so the caller can always show the actual JD/resume-extracted
        # degree string instead of a "no qualifying degree" placeholder that
        # reads as "this candidate has no degree" when the real reason is
        # simply "the JD has no requirement" or "the level was unparseable".
        required_degree_display = required_degree_text or (jd_extracted_education or {}).get("degree")
        candidate_degree_display = self._best_candidate_degree_text(candidate_education_entries)

        required_level = self._required_level(required_degree_text, jd_extracted_education)
        if required_level is None:
            return self._education_result(
                applicable=False, skipped=True, data_missing=False, passed=True, score=100.0,
                required_level=None, candidate_level=None, equivalent_experience_applied=False,
                required_degree_text=required_degree_display, candidate_degree_text=candidate_degree_display,
            )

        ranked_entries = [
            (level, entry) for level, entry in (
                (self._candidate_level(entry), entry) for entry in (candidate_education_entries or [])
            )
            if level is not None
        ]

        if not ranked_entries:
            if self._meets_equivalent_experience(candidate_total_years):
                return self._education_result(
                    applicable=True, skipped=False, data_missing=True, passed=True, score=100.0,
                    required_level=required_level, candidate_level=None, equivalent_experience_applied=True,
                    required_degree_text=required_degree_display, candidate_degree_text=candidate_degree_display,
                )
            return self._education_result(
                applicable=False, skipped=False, data_missing=True, passed=True, score=None,
                required_level=required_level, candidate_level=None, equivalent_experience_applied=False,
                required_degree_text=required_degree_display, candidate_degree_text=candidate_degree_display,
            )

        best_candidate_level, best_entry = max(ranked_entries, key=lambda pair: pair[0][0])
        passed = best_candidate_level[0] >= required_level[0]
        equivalent_experience_applied = False

        if not passed and self._meets_equivalent_experience(candidate_total_years):
            passed = True
            equivalent_experience_applied = True

        score = 100.0 if passed else round((best_candidate_level[0] / required_level[0]) * 100, 2)
        return self._education_result(
            applicable=True, skipped=False, data_missing=False, passed=passed, score=score,
            required_level=required_level, candidate_level=best_candidate_level,
            equivalent_experience_applied=equivalent_experience_applied,
            required_degree_text=required_degree_display,
            # The entry that actually decided pass/fail, not just "best" by
            # display preference - keeps the shown text consistent with the
            # level actually compared against the requirement.
            candidate_degree_text=(best_entry or {}).get("degree") or candidate_degree_display,
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
    def _education_result(
        *, applicable, skipped, data_missing, passed, score, required_level, candidate_level,
        equivalent_experience_applied, required_degree_text=None, candidate_degree_text=None,
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
        }
