from pydantic import BaseModel


class IvfflatIndexHealth(BaseModel):
    exists: bool
    index_name: str
    size_bytes: int | None
    scan_count: int | None


class EmbeddingDashboardResponse(BaseModel):
    resume_embeddings_count: int
    estimated_storage_bytes: int
    jd_embeddings_count: int
    active_embedding_model_name: str
    active_embedding_model_version: str
    ivfflat_index_health: IvfflatIndexHealth
    reindex_threshold: int
    reindex_warning: bool
    reindex_queued: bool
