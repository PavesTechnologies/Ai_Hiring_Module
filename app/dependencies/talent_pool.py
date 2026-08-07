from fastapi import Depends

from app.core.encryption_service import EncryptionService
from app.dependencies.campaign import get_jd_repository
from app.dependencies.campaign_candidate import (
    get_audit_service,
    get_campaign_candidate_repository,
    get_campaign_repository,
    get_celery_task_log_service,
)
from app.dependencies.resume import (
    get_candidate_repository,
    get_consent_repository,
    get_encryption_service,
    get_resume_repository,
)
from app.dependencies.skill_ontology import (
    get_config_repository,
    get_skill_ontology_repository,
    get_skill_repository,
)
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.candidate_scoring_service import CandidateScoringService
from app.services.campaign.resume_selection_service import ResumeSelectionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.talent_pool.talent_pool_service import TalentPoolService


# No pre-existing DI factory for CandidateScoringService anywhere in this
# codebase - it's only ever constructed inline inside Celery tasks (see
# deterministic_scoring_tasks.py). Added here, reusing the same three
# repositories that construction already relies on, so
# ResumeSelectionService can be resolved through the normal FastAPI
# dependency graph.
def get_candidate_scoring_service(
    skill_repo: SkillRepository = Depends(get_skill_repository),
    skill_ontology_repo: SkillOntologyRepository = Depends(get_skill_ontology_repository),
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> CandidateScoringService:
    return CandidateScoringService(skill_repo, skill_ontology_repo, config_repo)


def get_resume_selection_service(
    resume_repo: ResumeRepository = Depends(get_resume_repository),
    jd_repo: JDRepository = Depends(get_jd_repository),
    config_repo: ConfigRepository = Depends(get_config_repository),
    candidate_scoring_service: CandidateScoringService = Depends(get_candidate_scoring_service),
) -> ResumeSelectionService:
    return ResumeSelectionService(
        resume_repo=resume_repo,
        jd_repo=jd_repo,
        config_repo=config_repo,
        candidate_scoring_service=candidate_scoring_service,
    )


def get_talent_pool_service(
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    resume_repo: ResumeRepository = Depends(get_resume_repository),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    consent_repo: ConsentRepository = Depends(get_consent_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
    audit_service: AuditService = Depends(get_audit_service),
    celery_task_log_service: CeleryTaskLogService = Depends(get_celery_task_log_service),
    resume_selection_service: ResumeSelectionService = Depends(get_resume_selection_service),
) -> TalentPoolService:
    return TalentPoolService(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
        audit_service=audit_service,
        celery_task_log_service=celery_task_log_service,
        resume_selection_service=resume_selection_service,
    )
