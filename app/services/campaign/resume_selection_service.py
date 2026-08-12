from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

from app.exception_handler.exceptions import UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import HiringCampaign
from app.models.candidates import ParseStatus, Resume
from app.models.jd.job_descriptions import JobDescription
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService,
    MandatorySkillMatchType,
)
from app.services.campaign.experience_education_validation_service import (
    ExperienceEducationValidationService,
)
from app.services.resume.work_experience_duration import annotate_work_experience_durations

# Same platform_config keys deterministic_scoring_tasks.py already reads for
# the real pipeline - reused verbatim, never a second/duplicated key.
_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"
_EQUIVALENT_EXPERIENCE_YEARS_KEY = "EQUIVALENT_EXPERIENCE_YEARS"
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"
_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY = "RESUME_FRESHNESS_MAX_AGE_DAYS"

_DEFAULT_EXPERIENCE_TOLERANCE_YEARS = 0.0
# ~6 months - Talent Pool Eligibility: a resume older than this is never
# selected for a new campaign, regardless of is_talent_pool_eligible.
_DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS = 180
_DEFAULT_DETERMINISTIC_WEIGHT_SKILLS = 0.70
_DEFAULT_DETERMINISTIC_WEIGHT_EXPERIENCE = 0.15
_DEFAULT_DETERMINISTIC_WEIGHT_EDUCATION = 0.15

_NO_ELIGIBLE_RESUME_MESSAGE = (
    "Candidate has no eligible resume for campaign assignment - every resume version is "
    "either unparsed, not talent-pool eligible, or missing data required for scoring."
)


class SelectionMethod(str, Enum):
    # Exactly one eligible resume existed - it was used as-is, no
    # deterministic/semantic comparison was run (nothing to compare).
    DIRECT = "DIRECT"
    # More than one eligible resume existed - every one was scored and the
    # highest selection_score won.
    COMPARED = "COMPARED"


@dataclass(frozen=True)
class EvaluatedResume:
    resume: Resume
    deterministic_score: float | None
    deterministic_passed: bool | None
    semantic_score: float | None
    semantic_passed: bool | None
    selection_score: float | None
    is_selected: bool


@dataclass(frozen=True)
class ResumeSelectionResult:
    """
    The one, consistent return shape for both the DIRECT and COMPARED
    paths - callers branch on `selection_method` for display purposes only,
    never on which fields are populated. `evaluated_resumes` always
    contains every eligible resume (length 1 for DIRECT), so the audit
    trail always shows how many versions were actually in play.
    """
    selected_resume: Resume
    selection_method: SelectionMethod
    evaluated_resumes: list[EvaluatedResume]


class ResumeSelectionService:
    """
    M13-E01 S01 T03 - selects which of a candidate's resume versions gets
    attached to a new campaign_candidates row. Read-only: never persists
    anything (no campaign_candidates row exists yet at this point), and
    never writes to score_breakdown/semantic_score_breakdown - those remain
    exclusively owned by the real async pipeline once the winning resume's
    campaign_candidates row is created.

    Reuses the exact same scoring primitives the real pipeline uses
    (CandidateScoringService.build_mandatory_skill_breakdown +
    ExperienceEducationValidationService + CandidateScoringService.
    _combine_deterministic_score for deterministic; ResumeRepository.
    compute_semantic_similarity + the same clamp-to-zero/threshold logic
    SemanticScoringService uses, for semantic) - never a second,
    independent scoring formula. Deliberately bypasses the gated Celery
    tasks (which skip semantic entirely for a deterministic-failing
    candidate) since every eligible resume version must be scored
    unconditionally to be compared fairly.
    """

    def __init__(
        self,
        resume_repo: ResumeRepository,
        jd_repo: JDRepository,
        config_repo: ConfigRepository,
        candidate_scoring_service: CandidateScoringService,
    ):
        self.resume_repo = resume_repo
        self.jd_repo = jd_repo
        self.config_repo = config_repo
        self.candidate_scoring_service = candidate_scoring_service

    def select_resume_for_campaign(
        self, candidate_id: UUID, campaign: HiringCampaign,
    ) -> ResumeSelectionResult:
        eligible_resumes = [
            resume for resume in self.resume_repo.get_all_versions_by_candidate(candidate_id)
            if self._is_eligible(resume)
        ]

        if not eligible_resumes:
            raise UnprocessableError(_NO_ELIGIBLE_RESUME_MESSAGE)

        if len(eligible_resumes) == 1:
            resume = eligible_resumes[0]
            return ResumeSelectionResult(
                selected_resume=resume,
                selection_method=SelectionMethod.DIRECT,
                evaluated_resumes=[EvaluatedResume(
                    resume=resume,
                    deterministic_score=None,
                    deterministic_passed=None,
                    semantic_score=None,
                    semantic_passed=None,
                    selection_score=None,
                    is_selected=True,
                )],
            )

        return self._compare_and_select(eligible_resumes, campaign)

    def _is_eligible(self, resume: Resume) -> bool:
        if resume.parse_status != ParseStatus.PARSED:
            return False
        embedding = self.resume_repo.get_embedding(resume.id)
        if embedding is None or not embedding.is_talent_pool_eligible:
            return False
        return self._is_fresh(resume)

    def _is_fresh(self, resume: Resume) -> bool:
        """
        Talent Pool Eligibility (6-month rule) - a resume version older
        than RESUME_FRESHNESS_MAX_AGE_DAYS (default 180, ~6 months) is
        never eligible for campaign selection, independent of
        embedding.is_talent_pool_eligible (which tracks erasure/fraud
        state, not age). Computed dynamically from Resume.created_at on
        every call - never persisted, never cached, so a resume crossing
        the age threshold is excluded on its very next evaluation without
        any reconciliation job. created_at is set once at insert and never
        touched again (mirrors get_max_version_number's docstring on this),
        so age is naturally reset by any genuine resubmission, which always
        inserts a brand-new Resume row.
        """
        max_age_days = int(self.config_repo.get_configs_by_keys(
            [_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY],
        ).get(_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY) or _DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS)

        age = datetime.now(timezone.utc) - resume.created_at
        return age <= timedelta(days=max_age_days)

    def _compare_and_select(
        self, eligible_resumes: list[Resume], campaign: HiringCampaign,
    ) -> ResumeSelectionResult:
        job_description = self.jd_repo.get_by_id(campaign.jd_id)
        if job_description is None:
            raise CampaignException("Job description not found for this campaign.", 404)

        jd_has_embedding = self.jd_repo.get_embedding_by_jd_id(campaign.jd_id) is not None
        weight_deterministic, weight_semantic = self._selection_weights(campaign, jd_has_embedding)

        validation_service, min_experience_years, required_degree_text, score_weights = (
            self._experience_education_context(job_description)
        )

        evaluated = [
            self._evaluate_one(
                resume, campaign, job_description, jd_has_embedding,
                validation_service, min_experience_years, required_degree_text, score_weights,
                weight_deterministic, weight_semantic,
            )
            for resume in eligible_resumes
        ]

        winner = max(evaluated, key=lambda entry: entry.selection_score)
        finalized = [replace(entry, is_selected=entry is winner) for entry in evaluated]
        selected_resume = next(entry.resume for entry in finalized if entry.is_selected)

        return ResumeSelectionResult(
            selected_resume=selected_resume,
            selection_method=SelectionMethod.COMPARED,
            evaluated_resumes=finalized,
        )

    @staticmethod
    def _selection_weights(campaign: HiringCampaign, jd_has_embedding: bool) -> tuple[float, float]:
        """
        Renormalizes the campaign's own weight_deterministic/weight_semantic
        to sum to 100, since AI is never part of resume selection - this
        keeps a JD's real configured priorities driving the ranking rather
        than an arbitrary fixed split. Falls back to 100% deterministic
        when the JD has no embedding yet (semantic is unavailable for
        every resume equally - a campaign-wide condition, not a per-resume
        one) or when both weights are 0 (a campaign can legally weight
        100% on AI alone per the DB check constraint).
        """
        weight_deterministic = float(campaign.weight_deterministic)
        weight_semantic = float(campaign.weight_semantic) if jd_has_embedding else 0.0

        total = weight_deterministic + weight_semantic
        if total <= 0:
            return 100.0, 0.0

        return (weight_deterministic / total) * 100, (weight_semantic / total) * 100

    def _experience_education_context(
        self, job_description: JobDescription,
    ) -> tuple[ExperienceEducationValidationService, float | None, str | None, dict]:
        configs = self.config_repo.get_configs_by_keys([
            _EXPERIENCE_TOLERANCE_YEARS_KEY,
            _EQUIVALENT_EXPERIENCE_YEARS_KEY,
            _DETERMINISTIC_WEIGHT_SKILLS_KEY,
            _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY,
            _DETERMINISTIC_WEIGHT_EDUCATION_KEY,
        ])

        validation_service = ExperienceEducationValidationService(
            experience_tolerance_years=float(
                configs.get(_EXPERIENCE_TOLERANCE_YEARS_KEY, _DEFAULT_EXPERIENCE_TOLERANCE_YEARS)
            ),
            equivalent_experience_years=(
                float(configs[_EQUIVALENT_EXPERIENCE_YEARS_KEY])
                if configs.get(_EQUIVALENT_EXPERIENCE_YEARS_KEY) is not None else None
            ),
        )
        min_experience_years = (
            float(job_description.min_experience_years)
            if job_description.min_experience_years is not None else None
        )
        required_degree_text = (job_description.education_criteria or {}).get("degree")
        score_weights = {
            "skills": float(configs.get(_DETERMINISTIC_WEIGHT_SKILLS_KEY, _DEFAULT_DETERMINISTIC_WEIGHT_SKILLS)),
            "experience": float(
                configs.get(_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY, _DEFAULT_DETERMINISTIC_WEIGHT_EXPERIENCE)
            ),
            "education": float(
                configs.get(_DETERMINISTIC_WEIGHT_EDUCATION_KEY, _DEFAULT_DETERMINISTIC_WEIGHT_EDUCATION)
            ),
        }

        return validation_service, min_experience_years, required_degree_text, score_weights

    def _evaluate_one(
        self,
        resume: Resume,
        campaign: HiringCampaign,
        job_description: JobDescription,
        jd_has_embedding: bool,
        validation_service: ExperienceEducationValidationService,
        min_experience_years: float | None,
        required_degree_text: str | None,
        score_weights: dict,
        weight_deterministic: float,
        weight_semantic: float,
    ) -> EvaluatedResume:
        deterministic_score, deterministic_passed = self._score_deterministic(
            resume, campaign, job_description, validation_service,
            min_experience_years, required_degree_text, score_weights,
        )

        semantic_score, semantic_passed = (
            self._score_semantic(resume, campaign) if jd_has_embedding else (None, None)
        )

        # normalize_scores' own convention (CompositeScoringService): a raw
        # 0-1 semantic similarity is rescaled x100 before blending with a
        # 0-100 deterministic score. A resume whose own similarity couldn't
        # be computed (embedding-model-version mismatch, etc.) contributes
        # 0 for just that resume, rather than failing the whole comparison.
        semantic_component = (semantic_score * 100) if semantic_score is not None else 0.0
        selection_score = round(
            (weight_deterministic / 100) * deterministic_score
            + (weight_semantic / 100) * semantic_component,
            2,
        )

        return EvaluatedResume(
            resume=resume,
            deterministic_score=deterministic_score,
            deterministic_passed=deterministic_passed,
            semantic_score=semantic_score,
            semantic_passed=semantic_passed,
            selection_score=selection_score,
            is_selected=False,
        )

    def _score_deterministic(
        self,
        resume: Resume,
        campaign: HiringCampaign,
        job_description: JobDescription,
        validation_service: ExperienceEducationValidationService,
        min_experience_years: float | None,
        required_degree_text: str | None,
        score_weights: dict,
    ) -> tuple[float, bool]:
        """
        Mirrors CandidateScoringService.calculate_and_store_score_breakdown's
        exact skills+experience+education blend (never the skills-only
        calculate_deterministic_score shortcut), minus the persistence step
        - there is no campaign_candidates row to write to yet.
        """
        parsed_json = resume.parsed_json or {}
        # Same JSON-computed fallback as deterministic_scoring_tasks.py -
        # see annotate_work_experience_durations for why the date-computed
        # total is preferred over the raw (often null) extracted field.
        candidate_total_years = annotate_work_experience_durations(parsed_json).get("total_experience_years")
        candidate_education_entries = parsed_json.get("education")

        jd_extracted_education = (job_description.extracted_json or {}).get("education")
        jd_extracted_experience = (job_description.extracted_json or {}).get("experience")
        experience_result = validation_service.validate_experience(
            min_experience_years, candidate_total_years, jd_extracted_experience=jd_extracted_experience,
        )
        education_result = validation_service.validate_education(
            required_degree_text, candidate_education_entries, candidate_total_years,
            jd_extracted_education=jd_extracted_education,
        )

        breakdown = self.candidate_scoring_service.build_mandatory_skill_breakdown(job_description.id, resume.id)
        skill_score = breakdown["deterministic_score"]
        mandatory_skills_passed = not any(
            skill["match_type"] == MandatorySkillMatchType.MISSING.value
            for skill in breakdown["mandatory_skills"]
        )

        return self.candidate_scoring_service._combine_deterministic_score(
            skill_score, mandatory_skills_passed, experience_result, education_result,
            score_weights, float(campaign.deterministic_threshold),
        )

    def _score_semantic(self, resume: Resume, campaign: HiringCampaign) -> tuple[float | None, bool | None]:
        """
        Mirrors SemanticScoringService.calculate_and_store_semantic_score_breakdown's
        clamp-to-zero + threshold logic exactly, minus the persistence step
        and the matching/missing-skills derivation (that depends on a prior
        deterministic score_breakdown already stored on a campaign_candidates
        row, which doesn't exist yet here).
        """
        similarity = self.resume_repo.compute_semantic_similarity(resume.id, campaign.jd_id)
        if similarity is None:
            return None, None

        if similarity < 0.0:
            similarity = 0.0

        threshold = float(campaign.semantic_threshold)
        return similarity, similarity >= threshold
