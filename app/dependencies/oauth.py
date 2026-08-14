from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
from app.repositories.config_repository import ConfigRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.oauth_token_repository import OAuthTokenRepository
from app.services.google_calendar_service import GoogleCalendarService
from app.services.google_oauth_service import GoogleOAuthService
from app.services.microsoft_calendar_service import MicrosoftCalendarService
from app.services.microsoft_oauth_service import MicrosoftOAuthService


def get_oauth_token_repository(db: Session = Depends(get_db)) -> OAuthTokenRepository:
    return OAuthTokenRepository(db)


# Defined locally (not imported from app.dependencies.campaign_candidate)
# because app.dependencies.campaign_candidate will need to import
# get_microsoft_calendar_service from this module for
# get_interview_schedule_service's wiring - importing back from it here
# would be circular. Same reasoning already documented on
# get_encryption_key_repository in that file for the same problem with
# app.dependencies.resume.
def get_config_repository(db: Session = Depends(get_db)) -> ConfigRepository:
    return ConfigRepository(db)


def get_encryption_key_repository(db: Session = Depends(get_db)) -> EncryptionKeyRepository:
    return EncryptionKeyRepository(db)


def get_encryption_service(
    repository: EncryptionKeyRepository = Depends(get_encryption_key_repository),
) -> EncryptionService:
    return EncryptionService(repository)


def get_microsoft_oauth_service(
    oauth_token_repo: OAuthTokenRepository = Depends(get_oauth_token_repository),
    config_repo: ConfigRepository = Depends(get_config_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
) -> MicrosoftOAuthService:
    return MicrosoftOAuthService(oauth_token_repo, config_repo, encryption_service)


def get_microsoft_calendar_service(
    oauth_service: MicrosoftOAuthService = Depends(get_microsoft_oauth_service),
) -> MicrosoftCalendarService:
    return MicrosoftCalendarService(oauth_service)


def get_google_oauth_service(
    oauth_token_repo: OAuthTokenRepository = Depends(get_oauth_token_repository),
    config_repo: ConfigRepository = Depends(get_config_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
) -> GoogleOAuthService:
    return GoogleOAuthService(oauth_token_repo, config_repo, encryption_service)


def get_google_calendar_service(
    oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
) -> GoogleCalendarService:
    return GoogleCalendarService(oauth_service)
