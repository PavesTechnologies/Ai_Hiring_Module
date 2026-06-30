from uuid import UUID
from sqlalchemy.orm import Session
from app.models.job_descriptions import JobDescription


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
        
    def commit(self)->None:
        self.db.commit()
    
    def rollback(self)->None:
        self.db.rollback()
    
    
        
    
