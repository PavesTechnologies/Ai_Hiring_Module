from fastapi import Depends

from app.dependencies.campaign import get_config_repository
from app.dependencies.unknown_skill import get_skill_repository
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_repository import SkillRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.skills.unknown_skill_suggestion_service import UnknownSkillSuggestionService


def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()


def get_unknown_skill_suggestion_service(
    skill_repository: SkillRepository = Depends(get_skill_repository),
    config_repository: ConfigRepository = Depends(get_config_repository),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> UnknownSkillSuggestionService:
    return UnknownSkillSuggestionService(
        skill_repository=skill_repository,
        config_repository=config_repository,
        embedding_service=embedding_service,
    )
