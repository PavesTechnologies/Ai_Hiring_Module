from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import BadRequestError, NotFoundError
from app.models.skills import (
    JDSkillVerificationStatus,
    SkillOntology,
    UnknownSkill,
    UnknownSkillStatus,
)
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.skills.skill_normalization_service import SkillMatchTier


class SkillCurationService:
    """
    The HR-facing automation layer the finalized design calls for: resolving
    UnknownSkill entries (map to an existing canonical skill, or promote to
    a new one), remapping an existing JDSkill's canonical mapping, and
    alias enrichment. Sits alongside SkillNormalizationService/JDService
    rather than replacing either — normalization only ever produces
    matches; this service is the only thing that mutates the ontology or
    resolves an unknown skill after the fact.
    """

    def __init__(self, skill_repository: SkillRepository, audit_service: AuditService):
        self.skill_repository = skill_repository
        self.audit_service = audit_service

    def list_pending_unknown_skills(self) -> list[UnknownSkill]:
        return self.skill_repository.get_pending_unknown_skills()

    def map_to_existing_skill(
        self,
        unknown_skill_id: UUID,
        target_skill_id: UUID,
        actor_id: str,
        save_as_alias: bool = False,
    ) -> UnknownSkill:
        """
        HR decides raw_text is a variant of an already-canonical skill.
        Retroactively creates a JDSkill for every JD still linked to this
        UnknownSkill, resolves those links, and optionally records raw_text
        as a new alias of the target skill.
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)
        target_skill = self._get_skill_or_404(target_skill_id)

        self._create_retroactive_jd_skills(unknown_skill, target_skill.id)
        self.skill_repository.update_unknown_skill_status(
            unknown_skill, UnknownSkillStatus.MAPPED_TO_EXISTING
        )

        if save_as_alias:
            self._append_alias_validated(target_skill, unknown_skill.raw_text, actor_id)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.UNKNOWN_SKILL_MAPPED,
            entity_type=EntityType.UNKNOWN_SKILL,
            entity_id=unknown_skill.id,
            jurisdiction=None,
            details={
                "raw_text": unknown_skill.raw_text,
                "mapped_to_skill_id": str(target_skill.id),
                "saved_as_alias": save_as_alias,
            },
        )
        self.skill_repository.commit()
        return unknown_skill

    def promote_to_canonical(
        self,
        unknown_skill_id: UUID,
        actor_id: str,
        category: str | None = None,
    ) -> SkillOntology:
        """
        HR decides raw_text is a genuinely new skill. Creates it in
        SkillOntology, then resolves every JD still linked to this
        UnknownSkill exactly like map_to_existing_skill does.
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)

        existing = self.skill_repository.find_skill_by_name_or_alias(unknown_skill.raw_text)
        if existing:
            raise BadRequestError(
                f"'{unknown_skill.raw_text}' already exists in the skill ontology "
                f"as '{existing.canonical_name}' — map to it instead of promoting."
            )

        new_skill = self.skill_repository.create_skill_ontology(
            canonical_name=unknown_skill.raw_text,
            source="HR_PROMOTION",
            category=category,
        )

        self._create_retroactive_jd_skills(unknown_skill, new_skill.id)
        self.skill_repository.update_unknown_skill_status(
            unknown_skill, UnknownSkillStatus.PROMOTED_TO_CANONICAL
        )

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.UNKNOWN_SKILL_PROMOTED,
            entity_type=EntityType.SKILL_ONTOLOGY,
            entity_id=new_skill.id,
            jurisdiction=None,
            details={"raw_text": unknown_skill.raw_text, "canonical_skill_id": str(new_skill.id)},
        )
        self.skill_repository.commit()
        return new_skill

    def dismiss(self, unknown_skill_id: UUID, actor_id: str) -> UnknownSkill:
        """
        HR decides raw_text isn't a real skill (junk extraction, etc).
        Linked JDUnknownSkill rows are left PENDING — they're just never
        resolved further, since no JDSkill is ever created for them.
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)
        self.skill_repository.update_unknown_skill_status(unknown_skill, UnknownSkillStatus.DISMISSED)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.UNKNOWN_SKILL_DISMISSED,
            entity_type=EntityType.UNKNOWN_SKILL,
            entity_id=unknown_skill.id,
            jurisdiction=None,
            details={"raw_text": unknown_skill.raw_text},
        )
        self.skill_repository.commit()
        return unknown_skill

    def remap_jd_skill(self, jd_skill_id: UUID, new_canonical_skill_id: UUID, actor_id: str):
        """
        HR overrides an existing JDSkill's canonical mapping in place —
        updates canonical_skill_id only, no history column, per the
        finalized design; the prior mapping is recoverable from AuditLog.
        """
        jd_skill = self.skill_repository.get_jd_skill_by_id(jd_skill_id)
        if not jd_skill:
            raise NotFoundError(f"JDSkill with ID {jd_skill_id} not found.")

        new_skill = self._get_skill_or_404(new_canonical_skill_id)
        previous_skill_id = jd_skill.canonical_skill_id

        self.skill_repository.remap_jd_skill(jd_skill, new_skill.id)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.JD_SKILL_REMAPPED,
            entity_type=EntityType.JD_SKILL,
            entity_id=jd_skill.id,
            jurisdiction=None,
            details={
                "jd_id": str(jd_skill.jd_id),
                "previous_canonical_skill_id": str(previous_skill_id),
                "new_canonical_skill_id": str(new_skill.id),
            },
        )
        self.skill_repository.commit()
        return jd_skill

    def _create_retroactive_jd_skills(self, unknown_skill: UnknownSkill, canonical_skill_id: UUID) -> None:
        for link in self.skill_repository.get_pending_jd_links(unknown_skill.id):
            # Idempotency guard: a JD could in principle already have an
            # independently-matched JDSkill row for this same canonical
            # skill (unrelated to this unknown occurrence) — the DB's own
            # (jd_id, canonical_skill_id) unique constraint would reject a
            # blind insert, so check first rather than let that surface as
            # an unhandled IntegrityError mid-batch.
            if not self.skill_repository.get_jd_skill(link.jd_id, canonical_skill_id):
                self.skill_repository.create_jd_skill(
                    jd_id=link.jd_id,
                    canonical_skill_id=canonical_skill_id,
                    mandatory=bool(link.mandatory),
                    match_tier=SkillMatchTier.MANUAL_HR.value,
                    verification_status=JDSkillVerificationStatus.AUTO_VERIFIED,
                    confidence=1.0,
                )
                self.skill_repository.bump_occurrence_count(canonical_skill_id)
            self.skill_repository.mark_jd_unknown_skill_resolved(link)

    def _append_alias_validated(self, skill: SkillOntology, alias: str, actor_id: str) -> None:
        collision = self.skill_repository.find_skill_by_name_or_alias(alias)
        if collision and collision.id != skill.id:
            raise BadRequestError(
                f"'{alias}' cannot be added as an alias — it already belongs to "
                f"'{collision.canonical_name}'. Aliases must be globally unique."
            )
        if collision and collision.id == skill.id:
            return  # already an alias (or the canonical name) of this exact skill — no-op

        self.skill_repository.append_alias(skill, alias)
        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.ALIAS_ADDED,
            entity_type=EntityType.SKILL_ONTOLOGY,
            entity_id=skill.id,
            jurisdiction=None,
            details={"canonical_skill_id": str(skill.id), "alias": alias},
        )

    def _get_unknown_skill_or_404(self, unknown_skill_id: UUID) -> UnknownSkill:
        unknown_skill = self.skill_repository.get_unknown_skill_by_id(unknown_skill_id)
        if not unknown_skill:
            raise NotFoundError(f"UnknownSkill with ID {unknown_skill_id} not found.")
        return unknown_skill

    def _get_skill_or_404(self, skill_id: UUID) -> SkillOntology:
        skill = self.skill_repository.get_skill_by_id(skill_id)
        if not skill:
            raise NotFoundError(f"SkillOntology with ID {skill_id} not found.")
        return skill
