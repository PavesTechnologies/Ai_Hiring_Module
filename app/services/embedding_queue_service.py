import logging
from uuid import UUID, uuid4

from app.tasks.jd_embedding_tasks import generate_jd_embedding
from app.tasks.skill_ontology_tasks import generate_skill_embedding

logger = logging.getLogger(__name__)


class EmbeddingQueueError(Exception):
    """
    Raised when an embedding-generation job could not be queued (e.g. the
    Celery broker is unreachable). Carries skill_id/task_id so callers can
    log or handle the failure with the same context this service already
    logged internally.
    """

    def __init__(self, message: str, *, skill_id: UUID, task_id: UUID):
        super().__init__(message)
        self.skill_id = skill_id
        self.task_id = task_id


class JDEmbeddingQueueError(Exception):
    """
    Same shape as EmbeddingQueueError, for queue_jd_embedding - kept as its
    own exception type (rather than reusing EmbeddingQueueError, which is
    typed around skill_id) so callers can distinguish the two failure
    sources cleanly.
    """

    def __init__(self, message: str, *, jd_id: UUID, task_id: UUID):
        super().__init__(message)
        self.jd_id = jd_id
        self.task_id = task_id


class EmbeddingQueueService:
    """
    Single entry point for queuing embedding-generation Celery jobs —
    eliminates the task_id-generation + apply_async pattern that was
    previously duplicated in SkillOntologyService and the missing-embedding
    recovery script.

    Skill Ontology and JD embeddings are both supported today, as sibling
    `queue_<x>_embedding(...)` methods reusing this same shape (generate
    task_id, apply_async, structured logging, raise on failure) rather than
    duplicating it. Resume/Campaign embeddings are expected to land here
    the same way in future work.
    """

    def queue_jd_embedding(self, jd_id: UUID, force_regenerate: bool = False) -> UUID:
        """
        Queues the EMBED_JD task (generate_jd_embedding) for jd_id. Never
        computes an embedding itself. force_regenerate=False (default) is
        the "JD activated/updated" trigger - a cheap no-op if this JD
        already has an embedding. force_regenerate=True is the "this JD's
        jd_skills changed" trigger - always rebuilds the embedding text and
        overwrites the existing row for this jd_id if the content actually
        changed. Returns the generated task_id on success; raises
        JDEmbeddingQueueError on failure (e.g. the broker is unreachable) -
        callers that must not fail their own operation because of a
        queuing hiccup are expected to catch this and continue.
        """
        task_id = uuid4()
        logger.info(
            "Queuing JD embedding generation | jd_id=%s task_id=%s force_regenerate=%s",
            jd_id, task_id, force_regenerate,
        )

        try:
            generate_jd_embedding.apply_async(
                kwargs={
                    "task_id": str(task_id),
                    "jd_id": str(jd_id),
                    "force_regenerate": force_regenerate,
                },
                task_id=str(task_id),
            )
        except Exception as exc:
            logger.exception(
                "Failed to queue JD embedding generation | jd_id=%s task_id=%s", jd_id, task_id,
            )
            raise JDEmbeddingQueueError(
                f"Failed to queue embedding generation for JD '{jd_id}': {exc}",
                jd_id=jd_id,
                task_id=task_id,
            ) from exc

        logger.info(
            "Queued JD embedding generation successfully | jd_id=%s task_id=%s", jd_id, task_id,
        )
        return task_id

    def queue_skill_embedding(self, skill_id: UUID) -> UUID:
        """
        Queues the existing generate_skill_embedding task for skill_id.
        Never computes an embedding itself. Returns the generated task_id
        on success; raises EmbeddingQueueError on failure (e.g. the broker
        is unreachable) — callers that must not fail their own operation
        because of a queuing hiccup (e.g. skill creation) are expected to
        catch this and continue, exactly as they already do today.
        """
        task_id = uuid4()
        logger.info(
            "Queuing skill embedding generation | skill_id=%s task_id=%s", skill_id, task_id,
        )

        try:
            generate_skill_embedding.apply_async(
                kwargs={"task_id": str(task_id), "skill_id": str(skill_id)},
                task_id=str(task_id),
            )
        except Exception as exc:
            logger.exception(
                "Failed to queue skill embedding generation | skill_id=%s task_id=%s", skill_id, task_id,
            )
            raise EmbeddingQueueError(
                f"Failed to queue embedding generation for skill '{skill_id}': {exc}",
                skill_id=skill_id,
                task_id=task_id,
            ) from exc

        logger.info(
            "Queued skill embedding generation successfully | skill_id=%s task_id=%s", skill_id, task_id,
        )
        return task_id
