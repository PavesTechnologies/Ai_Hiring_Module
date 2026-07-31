import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.enums.constants import EMBEDDING_DIM
from app.models.embeddings import EmbeddingModelVersion


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
        settings.embedding_model (EMBEDDING_MODEL in .env) is the one place
        a human declares the model — this mirrors that into the DB row at
        app startup so the two can never drift and the active row can never
        simply be missing. A no-op if the active row already matches. If
        .env now names a different model, the old row is deprecated rather
        than overwritten in place — resume/JD embeddings already generated
        under it keep a FK that still correctly describes what produced
        them — and a new active row is created for the new model.
        """
        current = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.is_active.is_(True))
            .first()
        )
        if current and current.model_name == model_name:
            return current

        if current:
            current.is_active = False
            current.deprecated_at = datetime.now(timezone.utc)
            # Flushed separately from the insert below: uq_embedding_model_versions_active
            # only allows one is_active=TRUE row at a time, and an UPDATE/INSERT
            # in the same flush batch has no guaranteed ordering against it.
            self.db.flush()

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
