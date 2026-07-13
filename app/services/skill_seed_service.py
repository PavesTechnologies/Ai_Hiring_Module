import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.skills import SkillOntology

logger = logging.getLogger(__name__)


class SkillSeedService:
    """
    Inserts parsed Skill Ontology rows into the database.

    This service is reusable for:
    - Initial Seed Script
    - Future Bulk Import feature
    """

    def __init__(self, db: Session):
        self.db = db

    def seed_skills(self, skills: list[dict[str, Any]]) -> dict[str, int]:
        inserted = 0
        skipped = 0
        failed = 0

        try:
            logger.info("Loading existing skills...")

            # Load all existing canonical names once
            existing_names = {
                name
                for (name,) in self.db.query(
                    SkillOntology.canonical_name
                ).all()
            }

            logger.info("Loading parent skill mapping...")

            # Load all parent skill IDs once
            parent_lookup = {
                skill.canonical_name: skill.id
                for skill in self.db.query(SkillOntology).all()
            }

            logger.info("Processing %s skills...", len(skills))

            for skill in skills:

                canonical_name = skill["canonical_name"]

                # Skip duplicate skills
                if canonical_name in existing_names:
                    skipped += 1
                    continue

                try:
                    parent_skill_id = None

                    if skill["parent_skill"]:
                        parent_skill_id = parent_lookup.get(
                            skill["parent_skill"]
                        )

                    new_skill = SkillOntology(
                        id=uuid.uuid4(),
                        canonical_name=canonical_name,
                        aliases=skill["aliases"],
                        category=skill["category"],
                        parent_skill_id=parent_skill_id,
                        confidence=skill["confidence"],
                        source=skill["source"],
                        is_active=skill["is_active"],
                    )

                    self.db.add(new_skill)

                    # Update caches so duplicate parent references
                    # within the same Excel work correctly.
                    existing_names.add(canonical_name)
                    parent_lookup[canonical_name] = new_skill.id

                    inserted += 1

                except Exception:
                    failed += 1
                    logger.exception(
                        "Failed to prepare skill '%s'.",
                        canonical_name,
                    )

            logger.info("Committing transaction...")

            self.db.commit()

            logger.info(
                "Skill ontology seed completed. "
                "Inserted=%s Skipped=%s Failed=%s",
                inserted,
                skipped,
                failed,
            )

        except Exception:
            self.db.rollback()

            logger.exception(
                "Skill ontology seed failed. Transaction rolled back."
            )

            raise

        return {
            "inserted": inserted,
            "skipped": skipped,
            "failed": failed,
        }