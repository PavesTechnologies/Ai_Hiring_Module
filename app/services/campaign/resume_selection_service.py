from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

from app.exception_handler.exceptions import UnprocessableError
from app.models.campaigns import HiringCampaign
from app.models.candidates import ParseStatus, Resume
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository

_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY = "RESUME_FRESHNESS_MAX_AGE_DAYS"

# ~6 months - Talent Pool Eligibility: a resume older than this is never
# selected for a new campaign, regardless of is_talent_pool_eligible.
_DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS = 180

_NO_ELIGIBLE_RESUME_MESSAGE = (
    "Candidate has no eligible resume for campaign assignment - every resume version is "
    "either unparsed, not talent-pool eligible, or missing data required for scoring."
)


class SelectionMethod(str, Enum):
    # Exactly one eligible resume existed - it was used as-is, no
    # deterministic/semantic comparison was run (nothing to compare).
    DIRECT = "DIRECT"
    # More than one eligible resume existed - every one was scored and the highest selection_score won
    # (v1 scoring, removed in retire v1, phase E). Kept only so older audit entries still read.
    COMPARED = "COMPARED"
    # More than one eligible resume existed - the latest parsed version was used
    # (retire v1, phase C: the user's choice, 2026-11-02; no scoring involved).
    LATEST = "LATEST"


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
    attached to a new campaign_candidates row: the only eligible one, else the
    latest parsed version (retire v1, phase C - the user's choice; nothing is
    scored to pick it). Read-only: never persists anything.
    """

    def __init__(self, resume_repo: ResumeRepository, jd_repo: JDRepository, config_repo: ConfigRepository):
        self.resume_repo = resume_repo
        self.jd_repo = jd_repo
        self.config_repo = config_repo

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

        # The latest parsed version (highest version number, then newest) - scoring each version
        # with v1 to pick one ended with retire v1, phase C.
        latest = max(eligible_resumes, key=lambda r: (r.version_number or 0, r.created_at))
        return ResumeSelectionResult(
            selected_resume=latest,
            selection_method=SelectionMethod.LATEST,
            evaluated_resumes=[EvaluatedResume(
                resume=resume, deterministic_score=None, deterministic_passed=None, semantic_score=None,
                semantic_passed=None, selection_score=None, is_selected=resume is latest,
            ) for resume in eligible_resumes],
        )

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
