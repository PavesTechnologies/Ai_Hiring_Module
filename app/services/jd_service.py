from app.models.job_descriptions import (
    JobDescription, JDSourceFormat, EmbeddingStatus
)

from app.repositories.jd_repository import JDRepository
from app.schemas.jd.repondse import CreateJDResponse
from app.schemas.jd.request import CreateJDRequest
from app.services.hash_service import HashService

class JDService:
    def __init__(self, repository: JDRepository, hash_service: HashService):
        self.repository = repository,
        self.hash_service = hash_service
        


    def create_job(
        self,
        request: CreateJDRequest,
        created_by: str,
    )-> CreateJDResponse:
        try:
            
            job_description = JobDescription(
                title=request.title,
                raw_text=request.raw_text,
                jurisdiction=request.jurisdiction,
                min_experience_years=request.min_experience_years,
                education_criteria=(
                    request.education_criteria.model_dump()
                    if request.education_criteria else None
                ),
                source_format=JDSourceFormat.TEXT,
                version_number=1,
                is_active_version=True,
                content_hash=self.hash_service.generate_hash(request.raw_text),
                created_by=created_by,
            )
            job_description = self.repository.create_job_description(job_description)
            self.repository.commit()
            
            return CreateJDResponse(
                id=job_description.id,
                title=job_description.title,
                version_number=job_description.version_number,
                source_format=job_description.source_format.value,
                jurisdiction=job_description.jurisdiction
            )
        except Exception as e:
            self.repository.rollback()
            raise e
    