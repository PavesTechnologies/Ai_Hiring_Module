import logging
from typing import Optional

from app.repositories.skill_repository import SkillRepository
from app.schemas.unknown_skill.unknown_skill_response import (
    UnknownSkillPageResponse,
    UnknownSkillResponse,
)

logger = logging.getLogger(__name__)


class UnknownSkillService:
    """Business logic for listing Unknown Skills awaiting HR review."""

    def __init__(self, repository: SkillRepository):
        self.repository = repository

    def get_unknown_skills(
        self,
        *,
        page: int,
        page_size: int,
        search: Optional[str],
        status: Optional[str],
    ) -> UnknownSkillPageResponse:
        logger.info("Unknown skills request received | page=%s page_size=%s", page, page_size)
        logger.info("Filters applied | search=%s status=%s", search, status)

        skills = self.repository.list_unknown_skills(
            page=page, page_size=page_size, search=search, status=status
        )
        total = self.repository.count_unknown_skills(search=search, status=status)

        logger.info("Total records | total=%s", total)

        items = [
            UnknownSkillResponse(
                id=skill.id,
                skill_name=skill.raw_text,
                status=skill.status,
                created_at=skill.created_at,
                updated_at=skill.last_seen,
            )
            for skill in skills
        ]

        response = UnknownSkillPageResponse(items=items, page=page, page_size=page_size, total=total)
        logger.info("Successful response | returned=%s total=%s", len(items), total)
        return response
