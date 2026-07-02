from app.exceptions.duplicate_jd_exception import DuplicateJDException
from fastapi import HTTPException
from app.models.jd.job_descriptions import JobDescription, JDSourceFormat
from app.repositories.jd_repository import JDRepository
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest, JDSearchRequest
from app.schemas.jd.response import CreateJDResponse, UpdateJDResponse, JDListItem, PaginatedJDResponse
from app.services.jd.hash_service import HashService
from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo, ExistingJDInfo
from app.services.audit_service import AuditService
from app.enums.constants import ActionType, EntityType
from app.schemas.jd.response import GetJDResponse
from uuid import UUID
from app.exception_handler.exceptions import NotFoundError, BadRequestError
from app.mappers.jd_mapper import JDMapper



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
            job_description = self._build_job_description(
                request=request,
                create_by=created_by,
                version_number=1,
                parent_jd_id=None,
                lineage_root_id=None
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
                created_by=job_description.created_by,
            )

        except Exception:
            self.repository.rollback()
            raise
        
    
    
    def _build_job_description(self,
                               request: CreateJDRequest,
                               *,
                               create_by: str,
                               version_number: int,
                               parent_jd_id: UUID| None,
                               lineage_root_id: UUID | None) -> JobDescription:
        return JobDescription(
            title= request.title,
            raw_text= request.raw_text,
            jurisdiction= request.jurisdiction,
            min_experience_years= request.min_experience_years,
            education_criteria= (
                request.education_criteria.model_dump()
                if request.education_criteria
                else None
            ),
            source_format= JDSourceFormat.TEXT,
            content_hash= self.hash_service.generate_hash(request.raw_text),
            version_number= version_number,
            is_active_version= True,
            parent_jd_id= parent_jd_id,
            lineage_root_id= lineage_root_id,
            created_by= create_by
        )
         
    
        
    def get_by_id(self, jd_id: str) -> JobDescription | None:
        job_description = self.repository.get_by_id(jd_id=jd_id)
        
        if not job_description:
            return HTTPException(
                status_code=404,
                detail=f"Job Description with ID {jd_id} not found."
            )
            
        return GetJDResponse(
            created_at=job_description.created_at,
            created_by=job_description.created_by,
            id=job_description.id,
            is_active_version=job_description.is_active_version,
            jurisdiction=job_description.jurisdiction,
            min_experience_years=job_description.min_experience_years,
            raw_text=job_description.raw_text,
            required_skills=job_description.required_skills,
            source_format=job_description.source_format.value,            
            title=job_description.title,
            updated_at=job_description.updated_at,
            version_number=job_description.version_number,
            education_criteria=job_description.education_criteria,
            parsed_skills=job_description.parsed_skills
        )
        
    def get_all_jds(self, is_active_version: bool) -> list[JobDescription]:
        return self.repository.get_all_jds(is_active_version=is_active_version)
    
    
    
    def update_jd(
        self,
        jd_id: UUID,
        request: UpdateJDRequest,
        updated_by: str,
    )-> UpdateJDResponse:
        
        existing_jd = self.repository.get_by_id(jd_id=jd_id)
        
        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")
            
        if not existing_jd.is_active_version:
            raise BadRequestError(f"Cannot update an inactive version of the Job Description with ID {jd_id}.")
        
        if existing_jd.lineage_root_id:
            lineage_root_id = existing_jd.lineage_root_id
        else:
            lineage_root_id = existing_jd.id
            
        self.repository.deactivate_version(existing_jd)
        
        new_jd = self._build_job_description(
            request= request,
            create_by= updated_by,
            version_number= existing_jd.version_number + 1,
            parent_jd_id= existing_jd.id,
            lineage_root_id= lineage_root_id
        )
        
        new_jd = self.repository.create_job_description(new_jd)
        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type= ActionType.JD_VERSION_CREATED,
            entity_type= EntityType.JOB_DESCRIPTION,
            entity_id=new_jd.id,
            jurisdiction=new_jd.jurisdiction,
            details={
                "title": new_jd.title,
                "version_number": new_jd.version_number,
                "source_format": new_jd.source_format.value,
            }
        )
        
        self.repository.commit()
        
        return UpdateJDResponse(
            id= new_jd.id,
            title= new_jd.title,
            version_number= new_jd.version_number,
            updated_by= updated_by,
        )
        
    def deactivate_jd(self, jd_id: UUID, updated_by:str) -> UpdateJDResponse:
        existing_jd = self.repository.get_by_id(jd_id=jd_id)
        
        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")
            
        if not existing_jd.is_active_version:
            raise BadRequestError(f"Job Description with ID {jd_id} is already inactive.")
        
        self.repository.deactivate_version(existing_jd)
        
        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type= ActionType.JD_CLOSED,
            entity_type= EntityType.JOB_DESCRIPTION,
            entity_id=existing_jd.id,
            jurisdiction=existing_jd.jurisdiction,
            details={
                "title": existing_jd.title,
                "version_number": existing_jd.version_number,
                "source_format": existing_jd.source_format.value,
            }
        )
        
        self.repository.commit()
        
        return UpdateJDResponse(
            id= existing_jd.id,
            title= existing_jd.title,
            version_number= existing_jd.version_number,
            updated_by= updated_by,
        )
        
    def search_job_descriptions(
        self,
        request: JDSearchRequest,
    )-> PaginatedJDResponse:
        records, total = self.repository.search(request=request)
        
        items = [JDMapper.to_list_item(jd) for jd in records]
        
        return PaginatedJDResponse(
            total=total,
            page=request.page,
            size=request.size,
            items=items
        )
        