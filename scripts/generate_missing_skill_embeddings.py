"""
Missing Skill Embedding Recovery.

Some skill_ontology rows were created before the Celery worker existed (or
was running), so they never got queued for embedding generation and are
stuck showing "Pending" in the UI (embedding IS NULL). This script finds
every such row and queues embedding generation for it via
EmbeddingQueueService — the single entry point for embedding Celery jobs —
so this stays reusable for any future gap of the same shape (e.g. after a
bulk import or a migration that adds rows while the worker is down), and
never duplicates the task_id/apply_async mechanics itself.

Safe to re-run: it only ever selects rows where embedding IS NULL, so a
skill already recovered (or successfully processed by the time this runs
again) is simply not selected next time.
"""

import logging

from app.db.session import SessionLocal
from app.models.skills import SkillOntology
from app.services.embedding_queue_service import EmbeddingQueueError, EmbeddingQueueService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

db = SessionLocal()
embedding_queue_service = EmbeddingQueueService()

try:
    missing_skills = (
        db.query(SkillOntology)
        .filter(SkillOntology.embedding.is_(None))
        .all()
    )

    found = len(missing_skills)

    print("=========================================")
    print("Missing Skill Embedding Recovery")
    print("=========================================")

    if found == 0:
        print("No skills require embedding generation.")
    else:
        logger.info("Found %s skill(s) with a missing embedding.", found)

        queued = 0
        skipped = 0
        failed = 0

        for skill in missing_skills:
            # Inactive (soft-deleted) skills are hidden from search/
            # normalization/the active list — generating an embedding for
            # one would be wasted compute, so it's skipped rather than
            # queued or treated as a failure.
            if not skill.is_active:
                skipped += 1
                logger.info(
                    "Skipped inactive skill | skill_id=%s canonical_name=%s",
                    skill.id, skill.canonical_name,
                )
                continue

            try:
                task_id = embedding_queue_service.queue_skill_embedding(skill.id)
                queued += 1
                logger.info(
                    "Queued embedding generation | skill_id=%s canonical_name=%s task_id=%s",
                    skill.id, skill.canonical_name, task_id,
                )
            except EmbeddingQueueError:
                failed += 1
                logger.exception(
                    "Failed to queue embedding generation | skill_id=%s canonical_name=%s",
                    skill.id, skill.canonical_name,
                )

        print()
        print(f"Found Missing Skills : {found}")
        print()
        print(f"Queued Successfully : {queued}")
        print()
        print(f"Skipped : {skipped}")
        print()
        print(f"Failed : {failed}")
        print()
        print("Recovery Completed")

except Exception:
    db.rollback()
    logger.exception("Missing skill embedding recovery failed.")
    raise
finally:
    db.close()
