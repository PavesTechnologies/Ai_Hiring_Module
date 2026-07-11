from uuid import UUID
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.campaigns import HiringCampaign
from app.models.embeddings import EmbeddingModelVersion
from app.models.jd.job_descriptions import JDEmbedding, JobDescription
from app.models.jd.job_descriptions import EmbeddingStatus
from app.schemas.jd.request import JDSearchRequest
from app.models.identity import User

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
    
    def export_jd_list(
        self,
        request: JDSearchRequest,
    ) -> list[JobDescription]:

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

        sort_columns = {
            "title": JobDescription.title,
            "created_at": JobDescription.created_at,
            "version_number": JobDescription.version_number,
        }

        sort_column = sort_columns.get(
            request.sort_by,
            JobDescription.created_at,
        )

        if request.order == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column)

        return query.all()

    def count_export_jd_list(
        self,
        request: JDSearchRequest,
    ) -> int:

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

        return query.count()


    def export_single_jd(
        self,
        jd_id: UUID,
    ) -> JobDescription | None:

        return (
            self.db.query(JobDescription)
            .filter(JobDescription.id == jd_id)
            .first()
        ) 
            
    def get_version_history(
        self,
        lineage_root_id: UUID,
    ):
        return (
            self.db.query(JobDescription)
            .filter(
                JobDescription.lineage_root_id == lineage_root_id
            )
            .order_by(JobDescription.version_number)
            .all()
        )
    
    def get_user_full_name(
        self,
        user_id: str,
    ) -> str:

        user = (
            self.db.query(User)
            .filter(User.id == user_id)
            .first()
        )

        return user.full_name if user else ""

    def get_linked_campaign_count(
        self,
        jd_id: UUID,
    ) -> int:

        return (
            self.db.query(func.count(HiringCampaign.id))
            .filter(HiringCampaign.jd_id == jd_id)
            .scalar()
        )
    
    def get_linked_campaigns(
        self,
        jd_id: UUID,
    ) -> list[HiringCampaign]:

        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.jd_id == jd_id)
            .order_by(HiringCampaign.created_at.desc())
            .all()
        )

    def get_active_embedding_model_version(self) -> EmbeddingModelVersion:
        version = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.is_active.is_(True))
            .first()
        )
        if not version:
            raise RuntimeError("No active embedding model version is configured.")
        return version

    def create_jd_embedding(
        self,
        jd_id: UUID,
        embedding: list[float],
        embedding_model_version_id: UUID,
        input_text_hash: str,
    ) -> JDEmbedding:
        jd_embedding = JDEmbedding(
            jd_id=jd_id,
            embedding=embedding,
            embedding_model_version_id=embedding_model_version_id,
            input_text_hash=input_text_hash,
            embedding_status=EmbeddingStatus.READY,
        )
        self.db.add(jd_embedding)
        self.db.flush()
        self.db.refresh(jd_embedding)
        return jd_embedding