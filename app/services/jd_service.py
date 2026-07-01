from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.models.job_descriptions import JobDescription, JDSourceFormat
from app.repositories.jd_repository import JDRepository
from app.schemas.jd.request import CreateJDRequest
from app.schemas.jd.repondse import CreateJDResponse
from app.services.hash_service import HashService
from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo, ExistingJDInfo
from app.services.audit_service import AuditService
from app.models.audit import ActionType, EntityType


class JDService:

    def __init__(
        self,
        repository: JDRepository,
        hash_service: HashService,
        audit_service: AuditService
    ):
        self.repository = repository
        self.hash_service = hash_service
        self.audit_service = audit_service

    def create_jd(
        self,
        request: CreateJDRequest,
        created_by: str,
    ) -> CreateJDResponse:

        try:
            # Step 1 - Generate SHA-256 Hash
            content_hash = self.hash_service.generate_hash(
                request.raw_text
            )

            # Step 2 - Check for duplicate JD
            existing_jd = self.repository.get_by_content_hash(
                content_hash
            )

            if existing_jd:
                raise DuplicateJDException(
                    DuplicateJDInfo(
                        message="Duplicate job description found.",
                        existing_jd=ExistingJDInfo(
                            id=existing_jd.id,
                            title=existing_jd.title,
                            version_number=existing_jd.version_number,
                            created_at=existing_jd.created_at
                        ),
                        actions=["View Existing", "Create New Version"]
                    )
                )

            # Step 3 - Create Job Description
            job_description = JobDescription(
                title=request.title,
                raw_text=request.raw_text,
                jurisdiction=request.jurisdiction,
                min_experience_years=request.min_experience_years,
                education_criteria=(
                    request.education_criteria.model_dump()
                    if request.education_criteria
                    else None
                ),
                source_format=JDSourceFormat.TEXT,
                version_number=1,
                is_active_version=True,
                content_hash=content_hash,
                created_by=created_by,
            )
            
        

            # Step 4 - Save
            job_description = self.repository.create_job_description(
                job_description
            )
            
                        
            # Step 5 - Audit
            self.audit_service.log(
               actor_id=created_by,
               actor_role="HR_ADMIN",
               action_type= ActionType.JD_CREATED,
               entity_type= EntityType.JOB_DESCRIPTION,
               entity_id=job_description.id,
               jurisdiction=job_description.jurisdiction,
               details={
                   "title": job_description.title,
                   "version_number": job_description.version_number,
                   "source_format": job_description.source_format.value,
                   },
               )


            self.repository.commit()

            # Step 6 - Response
            return CreateJDResponse(
                id=job_description.id,
                title=job_description.title,
                version_number=job_description.version_number,
                source_format=job_description.source_format.value,
                jurisdiction=job_description.jurisdiction,
            )

        except Exception:
            self.repository.rollback()
            raise