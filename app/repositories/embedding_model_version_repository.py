import logging
import uuid

from sqlalchemy.orm import Session

from app.enums.constants import EMBEDDING_DIM
from app.models.embeddings import EmbeddingModelVersion

logger = logging.getLogger(__name__)



def active_version_id_sql():
    """
    The active embedding model version's id, as a SQL scalar subquery (settings phase 4). Embeddings are kept
    per (item, model version) so a re-embed can build a new model's beside the old; every lookup takes the
    version scoring uses - the active one, until a re-embed completes and the new version is activated.
    """
    from sqlalchemy import select
    # At most one active row (uq_embedding_model_versions_active), so no LIMIT is needed.
    return select(EmbeddingModelVersion.id).where(EmbeddingModelVersion.is_active.is_(True)).scalar_subquery()


class EmbeddingModelVersionRepository:
    """
    Single source of truth for "which embedding model is active" — every
    embedding operation (resume, JD, skill ontology, unknown-skill
    suggestions) resolves the model to load through this repository instead
    of a hardcoded settings value, so they can never drift apart.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_active(self) -> EmbeddingModelVersion:
        version = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.is_active.is_(True))
            .first()
        )
        if not version:
            raise RuntimeError("No active embedding model version is configured.")
        return version

    def sync_active_from_settings(self, model_name: str) -> EmbeddingModelVersion:
        """
        Settings phase 4: the database decides the embedding model. settings.embedding_model (EMBEDDING_MODEL
        in .env) only seeds the first version on a fresh database; once a version is active, .env never
        replaces it - a model change is a planned action on the Settings page (a re-embed builds the new
        model's embeddings beside the old, then the new version is activated). A different .env value is only
        logged.
        """
        current = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.is_active.is_(True))
            .first()
        )
        if current is not None:
            if current.model_name != model_name:
                logger.warning("EMBEDDING_MODEL=%s differs from the active embedding model %s, which is kept: "
                               "change the model on the Settings page.", model_name, current.model_name)
            return current

        new_version = EmbeddingModelVersion(
            id=uuid.uuid4(),
            model_name=model_name,
            model_version="v1",
            vector_dimensions=EMBEDDING_DIM,
            distance_metric="cosine",
            is_active=True,
        )
        self.db.add(new_version)
        self.db.commit()
        self.db.refresh(new_version)
        return new_version
