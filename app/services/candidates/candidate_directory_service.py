import logging
from uuid import UUID

from app.core.encryption_service import DecryptionError, EncryptionService
from app.models.candidates import Candidate
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.candidate.candidate_directory_schema import (
    CandidateDirectoryItem,
    CandidateDirectoryResponse,
    CandidateResumeSummary,
)
from app.services.talent_pool.talent_pool_service import TalentPoolService

logger = logging.getLogger(__name__)

UNDECRYPTABLE_PLACEHOLDER = "[undecryptable]"


class CandidateDirectoryService:
    """
    Global Candidates directory (GET /candidates) - every candidate
    regardless of campaign or Talent Pool membership: with campaigns,
    without campaigns, Talent-Pool-eligible or not. Deliberately separate
    from TalentPoolService (which is scoped to Talent-Pool-eligible
    campaign-selection candidates) and CampaignCandidateService (which is
    scoped to one campaign's pipeline) - neither is modified by this class.
    Read-only: never selects a resume for a campaign, never computes a
    campaign score.
    """

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        resume_repo: ResumeRepository,
        encryption_service: EncryptionService,
    ):
        self.candidate_repo = candidate_repo
        self.resume_repo = resume_repo
        self.encryption_service = encryption_service

    def list_candidates(
        self,
        *,
        email_hash: str | None = None,
        jurisdiction: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> CandidateDirectoryResponse:
        candidates = self.candidate_repo.search(
            email_hash=email_hash, jurisdiction=jurisdiction, page=page, size=size,
        )
        total = self.candidate_repo.count_search(email_hash=email_hash, jurisdiction=jurisdiction)

        # Batched over the whole page - one query for every candidate's
        # active resume, one more for every one of those resumes' skills -
        # never one query per row.
        active_resume_by_candidate = self.resume_repo.get_active_by_candidate_ids(
            [candidate.id for candidate in candidates],
        )
        resume_ids = [resume.id for resume in active_resume_by_candidate.values()]
        skills_by_resume_id = self.resume_repo.get_canonical_skills_by_resume_ids(resume_ids)

        items = [
            self._to_directory_item(candidate, active_resume_by_candidate.get(candidate.id), skills_by_resume_id)
            for candidate in candidates
        ]

        return CandidateDirectoryResponse(items=items, total=total, page=page, size=size)

    def _to_directory_item(self, candidate: Candidate, resume, skills_by_resume_id: dict) -> CandidateDirectoryItem:
        # designation/experience/location extraction is TalentPoolService's
        # own pure staticmethod (no TalentPoolService instance needed,
        # zero modification to that class) - reused rather than
        # re-implementing the exact same parsed_json field reads here.
        designation, experience, location = TalentPoolService._extract_resume_fields(resume)

        return CandidateDirectoryItem(
            candidate_id=candidate.id,
            full_name=self._decrypt_full_name(candidate),
            email=self._decrypt_masked_email(candidate),
            designation=designation,
            location=location,
            experience=experience,
            jurisdiction=candidate.jurisdiction,
            resume=(
                CandidateResumeSummary(
                    resume_id=resume.id,
                    version_number=resume.version_number,
                    parse_status=resume.parse_status,
                    uploaded_at=resume.created_at,
                )
                if resume is not None else None
            ),
            skills=skills_by_resume_id.get(resume.id, []) if resume is not None else [],
            created_at=candidate.created_at,
        )

    def _decrypt_full_name(self, candidate: Candidate) -> str | None:
        if not candidate.full_name_encrypted:
            return None
        try:
            return self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
        except DecryptionError:
            logger.exception("Failed to decrypt candidate name for candidate_id=%s", candidate.id)
            return UNDECRYPTABLE_PLACEHOLDER

    def _decrypt_masked_email(self, candidate: Candidate) -> str | None:
        if not candidate.email_encrypted:
            return None
        try:
            email = self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
        except DecryptionError:
            logger.exception("Failed to decrypt candidate email for candidate_id=%s", candidate.id)
            return None
        # Masking algorithm is TalentPoolService's own staticmethod (no
        # instance needed) - reused verbatim so the two candidate-list
        # surfaces can never mask emails differently.
        return TalentPoolService._mask_email(email)
