from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.jd import get_audit_service, get_jd_calibration_service
from app.dependencies.skills import get_skill_curation_service
from app.dependencies.unknown_skill import get_skill_repository
from app.dependencies.unknown_skill_suggestion import get_embedding_service
from app.repositories.skill_repository import SkillRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.audit_service import AuditService
from app.services.jd.jd_calibration_service import JDCalibrationService
from app.services.jd.jd_calibration_skill_service import JDCalibrationSkillService
from app.services.jd.jd_unknown_skill_service import JDUnknownSkillService
from app.services.skills.skill_curation_service import SkillCurationService


def get_jd_unknown_skill_service(
    db: Session = Depends(get_db),
    skill_repository: SkillRepository = Depends(get_skill_repository),
    curation_service: SkillCurationService = Depends(get_skill_curation_service),
    calibration_service: JDCalibrationService = Depends(get_jd_calibration_service),
    audit_service: AuditService = Depends(get_audit_service),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> JDUnknownSkillService:
    return JDUnknownSkillService(db, skill_repository, curation_service, calibration_service, audit_service,
                                 embedding_service=embedding_service)


def get_jd_calibration_skill_service(
    db: Session = Depends(get_db),
    skill_repository: SkillRepository = Depends(get_skill_repository),
    curation_service: SkillCurationService = Depends(get_skill_curation_service),
    calibration_service: JDCalibrationService = Depends(get_jd_calibration_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> JDCalibrationSkillService:
    return JDCalibrationSkillService(db, skill_repository, curation_service, calibration_service, audit_service)
