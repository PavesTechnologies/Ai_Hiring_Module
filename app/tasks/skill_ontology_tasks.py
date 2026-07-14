import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import EMBEDDING_DIM, ActionType, EntityType
from app.models.skills import SkillOntology
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)


@celery_app.task(name="skill.generate_embedding")
def generate_skill_embedding(task_id: str, skill_id: str) -> None:
    """
    Background leg of skill creation: computes the all-MiniLM-L6-v2 embedding
    for a newly created skill (from canonical_name only) and persists it, so
    the Create Skill API never blocks on model inference.

    Embedding failures are logged and rolled back but never re-raised — a bad
    embedding run must not crash the worker or retry-storm; the skill row
    itself already committed successfully before this task was queued.
    """
    db = SessionLocal()
    task_log = None
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        task_log = task_log_service.create_log(
            task_id=task_id,
            task_type="SKILL_EMBEDDING_GENERATION",
        )

        skill = db.query(SkillOntology).filter(SkillOntology.id == skill_id).first()
        if not skill:
            task_log_service.mark_failure(task_log, f"Skill {skill_id} not found.")
            return

        logger.info(
            "Embedding generation started | skill_id=%s canonical_name=%s",
            skill.id,
            skill.canonical_name,
        )

        embedding = EmbeddingService().generate_embedding(skill.canonical_name)

        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"Expected a {EMBEDDING_DIM}-dimensional embedding, got {len(embedding)}."
            )

        logger.info(
            "Embedding generation completed | skill_id=%s dimension=%d",
            skill.id,
            len(embedding),
        )

        # Model inference above can take real time. Re-fetch a fresh row
        # right before writing instead of reusing the instance loaded at the
        # top of this task — if the skill was deleted while the embedding
        # was being computed, this becomes a clean "not found" outcome
        # instead of a StaleDataError from an UPDATE matching 0 rows.
        skill = db.query(SkillOntology).filter(SkillOntology.id == skill_id).first()
        if not skill:
            task_log_service.mark_failure(task_log, f"Skill {skill_id} no longer exists; embedding discarded.")
            return

        skill.embedding = embedding
        skill.embedding_updated_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Embedding updated successfully | skill_id=%s embedding_updated_at=%s",
            skill.id, skill.embedding_updated_at,
        )

        task_log_service.mark_success(task_log, summary=f"Embedding generated for skill {skill_id}.")

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Embedding generation failed | skill_id=%s task_id=%s", skill_id, task_id)

    finally:
        db.close()


@celery_app.task(name="skill.detect_duplicate_aliases")
def detect_duplicate_skill_aliases() -> None:
    """
    Scheduled audit (S04-T03): scans every skill_ontology row's aliases
    array and reports — never modifies — any alias string shared by more
    than one canonical skill. The application-layer check in
    SkillOntologyService._merge_aliases already prevents this going
    forward; this periodic scan catches anything pre-existing (seed data,
    bulk import, etc.) that bypassed it.
    """
    db = SessionLocal()
    task_log = None
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        audit_service = AuditService(AuditRepository(db))

        task_log = task_log_service.create_log(
            task_id=str(uuid4()),
            task_type="SKILL_ALIAS_DUPLICATE_AUDIT",
        )

        logger.info("Alias duplicate audit started")

        alias_owners: dict[str, list[tuple]] = {}
        for skill_id, canonical_name, aliases in db.query(
            SkillOntology.id, SkillOntology.canonical_name, SkillOntology.aliases
        ):
            for alias in aliases or []:
                alias_owners.setdefault(alias, []).append((skill_id, canonical_name))

        duplicates_found = 0
        detected_at = datetime.now(timezone.utc)

        for alias, owners in alias_owners.items():
            if len(owners) < 2:
                continue

            for i in range(len(owners)):
                for j in range(i + 1, len(owners)):
                    skill_a_id, skill_a_name = owners[i]
                    skill_b_id, skill_b_name = owners[j]
                    duplicates_found += 1

                    logger.warning(
                        "Duplicate alias detected | alias='%s' skill_a='%s' (id=%s) skill_b='%s' (id=%s)",
                        alias, skill_a_name, skill_a_id, skill_b_name, skill_b_id,
                    )

                    audit_service.log(
                        actor_id=None,
                        actor_role="SYSTEM",
                        action_type=ActionType.ALIAS_DUPLICATE_DETECTED,
                        entity_type=EntityType.SKILL,
                        entity_id=skill_a_id,
                        details={
                            "alias": alias,
                            "canonical_skill_a": {"id": str(skill_a_id), "canonical_name": skill_a_name},
                            "canonical_skill_b": {"id": str(skill_b_id), "canonical_name": skill_b_name},
                            "detected_at": detected_at.isoformat(),
                        },
                    )

        db.commit()

        if duplicates_found == 0:
            summary = "Alias duplicate audit completed: no duplicates found."
            logger.info(summary)
        else:
            summary = f"Alias duplicate audit completed: {duplicates_found} duplicate alias pair(s) found."
            logger.warning(summary)

        task_log_service.mark_success(task_log, summary=summary)

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Alias duplicate audit failed")

    finally:
        db.close()
