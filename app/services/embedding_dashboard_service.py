import logging

from app.models.async_tasks import TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository

logger = logging.getLogger(__name__)

_EMBEDDING_REINDEX_THRESHOLD_KEY = "EMBEDDING_REINDEX_THRESHOLD"
_DEFAULT_EMBEDDING_REINDEX_THRESHOLD = 50000

# 384-dimension vector, single-precision (4-byte) float components -
# matches EMBEDDING_DIM (app/enums/constants.py) and every embedding model
# this platform uses (all-MiniLM-L6-v2).
_EMBEDDING_DIMENSIONS = 384
_BYTES_PER_FLOAT32 = 4


class EmbeddingDashboardService:
    """
    Embedding Storage Dashboard: read-mostly aggregation of resume/JD
    embedding counts, estimated storage, the active embedding model, and
    ivfflat index health - reuses each repository's own existing/new
    counting and health-check methods rather than duplicating any query
    logic here. The one side effect (enqueueing REINDEX_IVFFLAT past
    EMBEDDING_REINDEX_THRESHOLD) is itself idempotent - see
    _maybe_queue_reindex.
    """

    def __init__(
        self,
        resume_repository: ResumeRepository,
        jd_repository: JDRepository,
        config_repository: ConfigRepository,
        celery_task_log_repository: CeleryTaskLogRepository,
    ):
        self.resume_repository = resume_repository
        self.jd_repository = jd_repository
        self.config_repository = config_repository
        self.celery_task_log_repository = celery_task_log_repository

    def get_dashboard(self) -> dict:
        resume_embeddings_count = self.resume_repository.count_embeddings()
        jd_embeddings_count = self.jd_repository.count_embeddings()
        active_model = self.resume_repository.get_active_embedding_model_version()
        index_health = self.resume_repository.get_ivfflat_index_health()
        threshold = self._read_reindex_threshold()

        reindex_warning = resume_embeddings_count > threshold
        reindex_queued = self._maybe_queue_reindex() if reindex_warning else False

        return {
            "resume_embeddings_count": resume_embeddings_count,
            "estimated_storage_bytes": resume_embeddings_count * _EMBEDDING_DIMENSIONS * _BYTES_PER_FLOAT32,
            "jd_embeddings_count": jd_embeddings_count,
            "active_embedding_model_name": active_model.model_name,
            "active_embedding_model_version": active_model.model_version,
            "ivfflat_index_health": index_health,
            "reindex_threshold": threshold,
            "reindex_warning": reindex_warning,
            "reindex_queued": reindex_queued,
        }

    def _read_reindex_threshold(self) -> int:
        raw = self.config_repository.get_configs_by_keys([_EMBEDDING_REINDEX_THRESHOLD_KEY]).get(
            _EMBEDDING_REINDEX_THRESHOLD_KEY,
        )
        if raw is None:
            return _DEFAULT_EMBEDDING_REINDEX_THRESHOLD
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid EMBEDDING_REINDEX_THRESHOLD platform_config value %r - falling back to default %s.",
                raw, _DEFAULT_EMBEDDING_REINDEX_THRESHOLD,
            )
            return _DEFAULT_EMBEDDING_REINDEX_THRESHOLD

    def _maybe_queue_reindex(self) -> bool:
        """
        Never dispatched twice concurrently - checks for an existing
        QUEUED/RUNNING REINDEX_IVFFLAT celery_task_log row first. A
        broker-dispatch failure is logged, never raised - a REINDEX
        hiccup must never break the dashboard read itself.
        """
        from app.tasks.reindex_tasks import REINDEX_IVFFLAT_TASK_TYPE, reindex_ivfflat_resume_embeddings

        already_in_flight = self.celery_task_log_repository.count_by_task_type_and_statuses(
            REINDEX_IVFFLAT_TASK_TYPE, [TaskStatus.QUEUED, TaskStatus.RUNNING],
        ) > 0
        if already_in_flight:
            logger.info("REINDEX_IVFFLAT already queued/running - not enqueueing another.")
            return False

        try:
            reindex_ivfflat_resume_embeddings.apply_async()
            logger.info("Queued REINDEX_IVFFLAT for resume_embeddings.")
            return True
        except Exception:
            logger.exception("Failed to enqueue REINDEX_IVFFLAT.")
            return False
