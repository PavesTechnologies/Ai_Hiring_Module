from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
from app.dependencies.campaign_candidate import (
    get_audit_service,
    get_campaign_candidate_repository,
    get_campaign_repository,
    get_candidate_repository,
    get_encryption_service,
    get_interview_schedule_repository,
)
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.services.audit_service import AuditService
from app.services.interview_feedback_service import InterviewFeedbackService


def get_interview_feedback_repository(db: Session = Depends(get_db)) -> InterviewFeedbackRepository:
    return InterviewFeedbackRepository(db)


def get_interview_feedback_service(
    interview_feedback_repo: InterviewFeedbackRepository = Depends(get_interview_feedback_repository),
    interview_schedule_repo: InterviewScheduleRepository = Depends(get_interview_schedule_repository),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> InterviewFeedbackService:
    return InterviewFeedbackService(
        interview_feedback_repo, interview_schedule_repo, campaign_candidate_repo,
        campaign_repo, candidate_repo, encryption_service, audit_service,
    )
