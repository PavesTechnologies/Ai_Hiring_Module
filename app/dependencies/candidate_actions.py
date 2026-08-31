from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.campaign_candidate import (
    get_audit_service,
    get_campaign_candidate_ai_evaluation_repository,
    get_campaign_candidate_repository,
    get_pipeline_transition_service,
)
from app.repositories.campaign_candidate_ai_evaluation_repository import (
    CampaignCandidateAIEvaluationRepository,
)
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.services.audit_service import AuditService
from app.services.campaign.bulk_stage_move_service import BulkStageMoveService
from app.services.campaign.candidate_note_service import CandidateNoteService
from app.services.campaign.override_revert_service import OverrideRevertService
from app.services.campaign.pipeline_transition_service import PipelineTransitionService


def get_candidate_note_service(
    db: Session = Depends(get_db),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> CandidateNoteService:
    # Reuses CampaignRepository.get_user_names — the same batch decrypt-and-map
    # the dashboard already uses, rather than a second name-resolution path.
    campaign_repo = CampaignRepository(db)
    return CandidateNoteService(
        note_repo=CandidateNoteRepository(db),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        user_name_resolver=campaign_repo.get_user_names,
    )


def get_override_revert_service(
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    audit_service: AuditService = Depends(get_audit_service),
    ai_evaluation_repo: CampaignCandidateAIEvaluationRepository = Depends(
        get_campaign_candidate_ai_evaluation_repository
    ),
) -> OverrideRevertService:
    return OverrideRevertService(
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        ai_evaluation_repo=ai_evaluation_repo,
    )


def get_bulk_stage_move_service(
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    pipeline_transition_service: PipelineTransitionService = Depends(get_pipeline_transition_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> BulkStageMoveService:
    return BulkStageMoveService(
        campaign_candidate_repo=campaign_candidate_repo,
        pipeline_transition_service=pipeline_transition_service,
        audit_service=audit_service,
    )
