from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.storage_service import StorageService
from app.db.session import get_db
from app.dependencies.campaign import get_config_repository
from app.dependencies.campaign_candidate import get_campaign_repository
from app.dependencies.storage import get_storage_service
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.config_repository import ConfigRepository
from app.services.bulk_upload.bulk_upload_service import BulkUploadService
from app.services.bulk_upload.zip_validation_service import ZipValidationService


def get_bulk_upload_job_repository(
    db: Session = Depends(get_db),
) -> BulkUploadJobRepository:
    return BulkUploadJobRepository(db)


def get_zip_validation_service(
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> ZipValidationService:
    return ZipValidationService(config_repo)


def get_bulk_upload_service(
    bulk_upload_job_repo: BulkUploadJobRepository = Depends(get_bulk_upload_job_repository),
    zip_validation_service: ZipValidationService = Depends(get_zip_validation_service),
    storage_service: StorageService = Depends(get_storage_service),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
) -> BulkUploadService:
    return BulkUploadService(
        bulk_upload_job_repo=bulk_upload_job_repo,
        zip_validation_service=zip_validation_service,
        storage_service=storage_service,
        campaign_repo=campaign_repo,
    )
