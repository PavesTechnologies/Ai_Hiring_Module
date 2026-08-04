from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.embeddings import EmbeddingModelVersion
from app.repositories.embedding_model_version_repository import EmbeddingModelVersionRepository
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

    def get_duplicate_excluding_lineage(
        self,
        content_hash: str,
        lineage_root_id: UUID,
    ) -> JobDescription | None:
       
        return (
            self.db.query(JobDescription)
            .filter(
                JobDescription.content_hash == content_hash,
                ~or_(
                    JobDescription.lineage_root_id == lineage_root_id,
                    JobDescription.id == lineage_root_id,
                ),
            )
            .first()
        )

    def has_active_campaign(self, jd_id: UUID) -> bool:
       
        return (
            self.db.query(HiringCampaign.id)
            .filter(
                HiringCampaign.jd_id == jd_id,
                HiringCampaign.status != CampaignStatus.CLOSED,
            )
            .first()
            is not None
        )

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
        if request.is_verified:
            query = query.filter(
                JobDescription.is_verified == request.is_verified
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

    def get_campaign_status_counts(
        self,
        jd_ids: list[UUID],
    ) -> dict[UUID, dict[str, int]]:
      
        counts = {jd_id: {"active": 0, "passed": 0} for jd_id in jd_ids}
        if not jd_ids:
            return counts

        rows = (
            self.db.query(
                HiringCampaign.jd_id,
                HiringCampaign.status,
                func.count(HiringCampaign.id),
            )
            .filter(HiringCampaign.jd_id.in_(jd_ids))
            .group_by(HiringCampaign.jd_id, HiringCampaign.status)
            .all()
        )
        for jd_id, campaign_status, count in rows:
            key = "passed" if campaign_status == CampaignStatus.CLOSED else "active"
            counts[jd_id][key] = count

        return counts
    
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
        return EmbeddingModelVersionRepository(self.db).get_active()

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

    def get_embedding_by_jd_id(self, jd_id: UUID) -> JDEmbedding | None:

        return (
            self.db.query(JDEmbedding)
            .filter(JDEmbedding.jd_id == jd_id)
            .first()
        )

    def count_embeddings(self) -> int:
        """Embedding Storage Dashboard - total jd_embeddings row count."""
        return self.db.query(func.count(JDEmbedding.id)).scalar() or 0

    def get_embedding_by_content_hash(
        self, content_hash: str, embedding_model_version_id: UUID,
    ) -> JDEmbedding | None:
        
        stmt = select(JDEmbedding).where(
            JDEmbedding.input_text_hash == content_hash,
            JDEmbedding.embedding_model_version_id == embedding_model_version_id,
        )
        return self.db.execute(stmt).scalars().first()

    def create_jd_embedding_idempotent(
        self,
        jd_id: UUID,
        embedding: list[float],
        embedding_model_version_id: UUID,
        content_hash: str,
    ) -> tuple[JDEmbedding, bool]:
       
        jd_embedding = JDEmbedding(
            jd_id=jd_id,
            embedding=embedding,
            embedding_model_version_id=embedding_model_version_id,
            input_text_hash=content_hash,
            embedding_status=EmbeddingStatus.READY,
        )
        try:
            with self.db.begin_nested():
                self.db.add(jd_embedding)
                self.db.flush()
            self.db.refresh(jd_embedding)
            return jd_embedding, True
        except IntegrityError:
            existing = self.get_embedding_by_jd_id(jd_id)
            return existing, False

    def replace_jd_embedding(
        self,
        jd_id: UUID,
        embedding: list[float],
        embedding_model_version_id: UUID,
        content_hash: str,
    ) -> JDEmbedding:
       
        existing = self.get_embedding_by_jd_id(jd_id)
        if existing is None:
            jd_embedding, _ = self.create_jd_embedding_idempotent(
                jd_id=jd_id,
                embedding=embedding,
                embedding_model_version_id=embedding_model_version_id,
                content_hash=content_hash,
            )
            return jd_embedding

        existing.embedding = embedding
        existing.embedding_model_version_id = embedding_model_version_id
        existing.input_text_hash = content_hash
        existing.created_at = datetime.now(timezone.utc)
        self.db.flush()
        self.db.refresh(existing)
        return existing