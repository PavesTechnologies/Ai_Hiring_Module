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
  an explicit multi-degree list - JD's education_criteria stores a single
  free-text degree requirement (e.g. "Bachelor's"), and a candidate holding
  ANY degree at or above that level satisfies it (so "Bachelor's, Master's,
  or PhD all acceptable" falls out of the hierarchy for free, satisfying
  the "multiple acceptable degrees" requirement without changing the JD
  schema/API).
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

_DEFAULT_EXPERIENCE_TOLERANCE_YEARS = 0.0
_DEFAULT_EQUIVALENT_EXPERIENCE_YEARS = 8.0

# Ordered low -> high. Matched by case-insensitive substring against a
# free-text degree string ("degree" on both JD.education_criteria and a
# candidate's parsed education entries) - first checked keyword wins its
# level, and a candidate's HIGHEST matched level is what counts.
_DEGREE_LEVELS = (
    (1, "HIGH_SCHOOL", ("high school", "secondary school", "hsc", "ssc", "10+2")),
    (2, "ASSOCIATE", ("associate", "diploma")),
    (3, "BACHELOR", ("bachelor", "b.sc", "bsc", "b.tech", "btech", "b.e.", "be ", "b.a.", "ba ", "undergraduate")),
    (4, "MASTER", ("master", "m.sc", "msc", "m.tech", "mtech", "mba", "m.a.", "ma ", "postgraduate")),
    (5, "DOCTORATE", ("phd", "ph.d", "doctorate", "d.phil")),
)


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
    ) -> dict:
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
    ) -> dict:
        required_level = self._degree_level(required_degree_text)
        if required_level is None:
            return self._education_result(
                applicable=False, skipped=True, data_missing=False, passed=True, score=100.0,
                required_level=None, candidate_level=None, equivalent_experience_applied=False,
            )

        candidate_levels = [
            level for level in (
                self._degree_level((entry or {}).get("degree"))
                for entry in (candidate_education_entries or [])
            )
            if level is not None
        ]

        if not candidate_levels:
            if self._meets_equivalent_experience(candidate_total_years):
                return self._education_result(
                    applicable=True, skipped=False, data_missing=True, passed=True, score=100.0,
                    required_level=required_level, candidate_level=None, equivalent_experience_applied=True,
                )
            return self._education_result(
                applicable=False, skipped=False, data_missing=True, passed=True, score=None,
                required_level=required_level, candidate_level=None, equivalent_experience_applied=False,
            )

        best_candidate_level = max(candidate_levels, key=lambda level: level[0])
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
        )

    def _meets_equivalent_experience(self, candidate_total_years: float | None) -> bool:
        return (
            self.equivalent_experience_years is not None
            and candidate_total_years is not None
            and candidate_total_years >= self.equivalent_experience_years
        )

    @classmethod
    def _degree_level(cls, degree_text: str | None) -> tuple[int, str] | None:
        if not degree_text:
            return None
        lowered = degree_text.lower()
        best = None
        for rank, name, keywords in _DEGREE_LEVELS:
            if any(keyword in lowered for keyword in keywords):
                if best is None or rank > best[0]:
                    best = (rank, name)
        return best

    @staticmethod
    def _education_result(
        *, applicable, skipped, data_missing, passed, score, required_level, candidate_level,
        equivalent_experience_applied,
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
        }
