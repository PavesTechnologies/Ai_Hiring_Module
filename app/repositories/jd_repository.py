from uuid import UUID
from sqlalchemy.orm import Session
from app.models.jd.job_descriptions import JobDescription
from app.schemas.jd.request import JDSearchRequest

class JDRepository:
    
    def __init__(self, db: Session):
        self.db = db
        
        
    def create_job_description(self, job_description: JobDescription) -> JobDescription:
        self.db.add(job_description)
        self.db.flush()
        self.db.refresh(job_description)
        return job_description
    
    
    def get_by_id(self, jd_id: UUID) -> JobDescription | None:
        return (
            self.db.query(JobDescription)
            .filter(JobDescription.id == jd_id)
            .first()
        )
        
        
    def get_by_content_hash(
        self,
        content_hash: str,
    ) -> JobDescription | None:
        return (
            self.db.query(JobDescription)
            .filter(JobDescription.content_hash == content_hash)
            .first()
        )
    
    def get_all_jds(self, is_active_version: bool) -> list[JobDescription]:
        return (
            self.db.query(JobDescription)
            .filter(JobDescription.is_active_version == is_active_version)
            .all()
        )
        
        
    def deactivate_version(self, job_description: JobDescription) -> None:
        job_description.is_active_version = False
        
    def get_latest_version(self, lineage_id: UUID) -> JobDescription | None:
        return (
            self.db.query(JobDescription)
            .filter(JobDescription.lineage_root_id == lineage_id
            ).order_by(JobDescription.version_number.desc())
            .first()
        )
    
    def search(
        self,
        request: JDSearchRequest,
    )-> tuple[list[JobDescription], int]:
        query = self.db.query(JobDescription)
        if request.search:
            query = query.filter(
                JobDescription.title.ilike(f"%{request.search}%")
            )
        if request.jurisdiction:
            query = query.filter(
                JobDescription.jurisdiction == request.jurisdiction
            )
        if request.active is not None:
            query = query.filter(
                JobDescription.is_active_version == request.active
            )
        if request.source_format:
            query = query.filter(
                JobDescription.source_format == request.source_format
            )
        total = query.count()
        sort_columns = {
            "title": JobDescription.title,
            "created_at": JobDescription.created_at,
            "version_number": JobDescription.version_number
        }
        
        sort_column = sort_columns.get(
            request.sort_by,
            JobDescription.created_at,
        )
        if request.order == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column)
        records = (
            query
            .offset((request.page - 1) * request.size)
            .limit(request.size)
            .all()
        )
        
        return records, total
        
    def commit(self)->None:
        self.db.commit()
    
    def rollback(self)->None:
        self.db.rollback()
    
    
        
    
