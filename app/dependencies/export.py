from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.export_repository import ExportRepository
from app.services.audit_service import AuditService
from app.services.export.export_service import ExportService
from app.services.export.scheduled_export_service import ScheduledExportService


def get_export_service(db: Session = Depends(get_db)) -> ExportService:
    return ExportService(
        export_repo=ExportRepository(db),
        campaign_repo=CampaignRepository(db),
        audit_service=AuditService(AuditRepository(db)),
        config_repo=ConfigRepository(db),
        # Recruiter notes are a column in the export; passing the repo
        # in keeps ExportService from importing it directly and lets the export
        # degrade gracefully when the notes migration has not been applied.
        note_repo=CandidateNoteRepository(db),
    )


def get_scheduled_export_service(db: Session = Depends(get_db)) -> ScheduledExportService:
    return ScheduledExportService(
        campaign_repo=CampaignRepository(db),
        export_service=get_export_service(db),
        audit_service=AuditService(AuditRepository(db)),
    )
