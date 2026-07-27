import logging
from uuid import UUID

from app.core.encryption_service import DecryptionError, EncryptionService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import BadRequestError, NotFoundError
from app.models.candidates import Candidate
from app.models.skills import (
    JDSkillVerificationStatus,
    SkillOntology,
    UnknownSkill,
    UnknownSkillStatus,
)
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.unknown_skill.skill_resolution_request import UnknownSkillResolutionType
from app.services.audit_service import AuditService
from app.services.embedding_queue_service import EmbeddingQueueError, EmbeddingQueueService
from app.services.jd.jd_service import _DEFAULT_JD_SKILL_WEIGHT
from app.services.skills.skill_normalization_service import (
    SkillMatchTier,
    scoring_weight_for_tier,
    verification_status_for_tier,
)

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        skill_repository: SkillRepository,
        audit_service: AuditService,
        embedding_queue_service: EmbeddingQueueService,
        encryption_service: EncryptionService,
        resume_repository: ResumeRepository,
    ):
        self.skill_repository = skill_repository
        self.audit_service = audit_service
        self.embedding_queue_service = embedding_queue_service
        self.encryption_service = encryption_service
        self.resume_repository = resume_repository

    def list_pending_unknown_skills(self) -> list[UnknownSkill]:
        return self.skill_repository.get_pending_unknown_skills()

    def list_jd_skills(self, jd_id: UUID):
        """Resolved (canonical) skills matched for a JD."""
        return self.skill_repository.get_jd_skills_by_jd_id(jd_id)

    def list_jd_unknown_skills(self, jd_id: UUID):
        """Unknown-skill occurrences recorded for a JD, resolved or not."""
        return self.skill_repository.get_jd_unknown_skills_by_jd_id(jd_id)

    def list_jds_for_unknown_skill(self, unknown_skill_id: UUID):
        """Every JD version where this UnknownSkill occurs — the reverse direction of list_jd_unknown_skills."""
        self._get_unknown_skill_or_404(unknown_skill_id)
        return self.skill_repository.get_jd_links_by_unknown_skill_id(unknown_skill_id)

    def list_candidates_for_unknown_skill(self, unknown_skill_id: UUID):
        """
        Candidates whose resume carries this exact unmatched raw skill text.
        Matched on raw_text, not id — CandidateSkill has no FK to
        unknown_skills (see get_candidate_skills_by_raw_text's own
        docstring for why), so this can only ever be a raw-text match, never
        a join through a shared unknown-skill row the way the JD side is.
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)
        return self.skill_repository.get_candidate_skills_by_raw_text(unknown_skill.raw_text)

    def decrypt_candidate_name(self, candidate: Candidate) -> str | None:
        if not candidate.full_name_encrypted:
            return None
        try:
            return self.encryption_service.decrypt(
                candidate.full_name_encrypted, candidate.encryption_key_id,
            )
        except DecryptionError:
            logger.exception("Failed to decrypt candidate name for candidate_id=%s", candidate.id)
            return None

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
        self._enqueue_skill_embedding(new_skill.id)
        return new_skill

    def create_canonical_skill_from_unknown(
        self,
        unknown_skill_id: UUID,
        actor_id: str,
        canonical_name: str,
        aliases: list[str] | None = None,
        category: str | None = None,
        parent_skill_id: UUID | None = None,
        confidence: str = "unverified",
        source: str = "manual entry",
        is_active: bool = True,
    ) -> dict:
        """
        Fuller variant of promote_to_canonical: HR supplies the canonical
        name plus the full set of SkillOntology fields (aliases/category/
        parent/confidence/source/is_active) up front instead of getting
        bare defaults derived from raw_text. Every JD and candidate
        occurrence still linked to the UnknownSkill is migrated onto the
        new skill as a verified (MANUAL_HR) match, and the UnknownSkill row
        itself - along with its JDUnknownSkill/CandidateSkill links - is
        then hard-deleted rather than left around with a terminal status.
        """
        result = self._create_canonical_skill_from_unknown_core(
            unknown_skill_id,
            actor_id,
            canonical_name=canonical_name,
            aliases=aliases,
            category=category,
            parent_skill_id=parent_skill_id,
            confidence=confidence,
            source=source,
            is_active=is_active,
        )
        self.skill_repository.commit()
        self._enqueue_skill_embedding(result["skill"].id)
        return result

    def bulk_approve_unknown_skills(self, unknown_skill_ids: list[UUID], actor_id: str) -> list[dict]:
        """
        Bulk version of create_canonical_skill_from_unknown: each id is
        promoted to its own new canonical skill (canonical_name is that
        UnknownSkill's own raw_text - bulk mode has no per-item field
        overrides), migrated and hard-deleted exactly like the single
        endpoint. Each id commits independently so one failure doesn't roll
        back ids already processed earlier in the batch; a failed id only
        rolls back its own (uncommitted) work so the next id starts from a
        clean transaction.
        """
        results = []
        for unknown_skill_id in unknown_skill_ids:
            try:
                result = self._create_canonical_skill_from_unknown_core(unknown_skill_id, actor_id)
                self.skill_repository.commit()
            except (NotFoundError, BadRequestError) as exc:
                self.skill_repository.rollback()
                results.append({
                    "unknown_skill_id": unknown_skill_id,
                    "success": False,
                    "message": str(exc),
                })
                continue

            self._enqueue_skill_embedding(result["skill"].id)
            results.append({
                "unknown_skill_id": unknown_skill_id,
                "success": True,
                "message": "Approved.",
                "canonical_skill_id": result["skill"].id,
                "canonical_name": result["skill"].canonical_name,
                "jd_skills_migrated": result["jd_skills_migrated"],
                "candidate_skills_migrated": result["candidate_skills_migrated"],
            })
        return results

    def _create_canonical_skill_from_unknown_core(
        self,
        unknown_skill_id: UUID,
        actor_id: str,
        canonical_name: str | None = None,
        aliases: list[str] | None = None,
        category: str | None = None,
        parent_skill_id: UUID | None = None,
        confidence: str = "unverified",
        source: str = "manual entry",
        is_active: bool = True,
    ) -> dict:
        """
        Uncommitted core shared by the single create-canonical endpoint and
        bulk approve - callers own commit()/rollback() and the embedding
        enqueue, since bulk approve needs those to happen per item rather
        than once for the whole batch. canonical_name defaults to the
        UnknownSkill's own raw_text when not supplied (bulk approve's case,
        which has no per-item field overrides).
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)

        resolved_name = (canonical_name if canonical_name is not None else unknown_skill.raw_text).strip()
        if not resolved_name:
            raise BadRequestError("canonical_name cannot be empty.")

        existing = self.skill_repository.find_skill_by_name_or_alias(resolved_name)
        if existing:
            raise BadRequestError(
                f"'{resolved_name}' already exists in the skill ontology "
                f"as '{existing.canonical_name}' — map to it instead of creating a new skill."
            )

        if parent_skill_id is not None:
            self._get_skill_or_404(parent_skill_id)

        cleaned_aliases = self._clean_aliases(aliases)
        for alias in cleaned_aliases:
            collision = self.skill_repository.find_skill_by_name_or_alias(alias)
            if collision:
                raise BadRequestError(
                    f"'{alias}' cannot be added as an alias — it already belongs to "
                    f"'{collision.canonical_name}'. Aliases must be globally unique."
                )

        new_skill = self.skill_repository.create_skill_ontology(
            canonical_name=resolved_name,
            source=source,
            category=category,
            aliases=cleaned_aliases,
            parent_skill_id=parent_skill_id,
            confidence=confidence,
            is_active=is_active,
        )

        migration = self._finalize_unknown_skill_resolution(unknown_skill, new_skill.id)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.UNKNOWN_SKILL_PROMOTED,
            entity_type=EntityType.SKILL_ONTOLOGY,
            entity_id=new_skill.id,
            jurisdiction=None,
            details={
                "unknown_skill_id": str(unknown_skill_id),
                "raw_text": unknown_skill.raw_text,
                "canonical_name": new_skill.canonical_name,
                "canonical_skill_id": str(new_skill.id),
                **migration,
            },
        )

        return {"skill": new_skill, **migration}

    def resolve_unknown_skill(
        self,
        unknown_skill_id: UUID,
        canonical_skill_id: UUID,
        resolution_type: UnknownSkillResolutionType,
        actor_id: str,
    ) -> None:
        """
        Unknown Skill Resolution API: migrates every JDUnknownSkill and
        CandidateSkill occurrence of this UnknownSkill onto the selected
        canonical skill, deleting the processed occurrence rows once each
        is migrated (duplicate-checked first), then marks the UnknownSkill
        MAPPED_TO_EXISTING - the existing status enum's value for "HR
        mapped this to an existing canonical skill"; there is no separate
        RESOLVED value. ADD_AS_ALIAS additionally records raw_text as a new
        alias of the target skill before migrating.

        Distinct from map_to_existing_skill(): that method marks
        JDUnknownSkill links RESOLVED and keeps them for audit/history.
        This endpoint's contract instead deletes the processed rows
        outright once migrated, so it reuses _create_retroactive_jd_skills
        for the JD side (zero duplicated logic) and just deletes each link
        it resolved as a follow-up step.
        """
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)
        target_skill = self._get_skill_or_404(canonical_skill_id)

        if unknown_skill.status != UnknownSkillStatus.PENDING:
            raise BadRequestError(
                f"Unknown skill '{unknown_skill_id}' is not pending "
                f"(status={unknown_skill.status.value})."
            )

        if resolution_type == UnknownSkillResolutionType.ADD_AS_ALIAS:
            self._append_alias_validated(target_skill, unknown_skill.raw_text, actor_id)

        jd_links = self.skill_repository.get_pending_jd_links(unknown_skill.id)
        jd_ids = {link.jd_id for link in jd_links}
        self._create_retroactive_jd_skills(unknown_skill, target_skill.id)
        for link in jd_links:
            self.skill_repository.delete_jd_unknown_skill(link)

        for candidate_skill in self.skill_repository.get_candidate_skills_by_unknown_skill_id(unknown_skill.id):
            if not self.skill_repository.get_candidate_skill_by_canonical(candidate_skill.resume_id, target_skill.id):
                self.resume_repository.create_candidate_skill(
                    candidate_id=candidate_skill.candidate_id,
                    resume_id=candidate_skill.resume_id,
                    canonical_skill_id=target_skill.id,
                    raw_extracted_text=candidate_skill.raw_extracted_text,
                    confidence=1.0,
                    match_tier=SkillMatchTier.MANUAL_HR.value,
                    status=verification_status_for_tier(SkillMatchTier.MANUAL_HR).value,
                    scoring_weight=scoring_weight_for_tier(SkillMatchTier.MANUAL_HR),
                )
                self.skill_repository.bump_occurrence_count(target_skill.id)
            self.skill_repository.delete_candidate_skill(candidate_skill)

        self.skill_repository.update_unknown_skill_status(unknown_skill, UnknownSkillStatus.MAPPED_TO_EXISTING)
        self._refresh_jd_verification_status(jd_ids)

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
                "resolution_type": resolution_type.value,
            },
        )
        self.skill_repository.commit()

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

    def delete_unknown_skill(self, unknown_skill_id: UUID, actor_id: str) -> dict:
        """
        Hard-deletes an UnknownSkill along with its JDUnknownSkill links and
        CandidateSkill occurrences (see SkillRepository.delete_unknown_skill_cascade
        for exactly what that touches). Unlike map/promote/dismiss, this is
        destructive and irreversible - allowed regardless of the unknown
        skill's current status.
        """
        result = self._delete_unknown_skill_core(unknown_skill_id, actor_id)
        self.skill_repository.commit()
        return result

    def bulk_delete_unknown_skills(self, unknown_skill_ids: list[UUID], actor_id: str) -> list[dict]:
        """
        Bulk version of delete_unknown_skill - each id commits independently
        so one failure doesn't roll back ids already deleted earlier in the
        batch (see bulk_approve_unknown_skills for the same reasoning).
        """
        results = []
        for unknown_skill_id in unknown_skill_ids:
            try:
                result = self._delete_unknown_skill_core(unknown_skill_id, actor_id)
                self.skill_repository.commit()
            except NotFoundError as exc:
                self.skill_repository.rollback()
                results.append({
                    "unknown_skill_id": unknown_skill_id,
                    "success": False,
                    "message": str(exc),
                })
                continue

            results.append({
                "unknown_skill_id": unknown_skill_id,
                "success": True,
                "message": "Deleted.",
                "jd_unknown_skills_deleted": result["jd_unknown_skills_deleted"],
                "candidate_skills_deleted": result["candidate_skills_deleted"],
            })
        return results

    def _delete_unknown_skill_core(self, unknown_skill_id: UUID, actor_id: str) -> dict:
        unknown_skill = self._get_unknown_skill_or_404(unknown_skill_id)
        raw_text = unknown_skill.raw_text

        counts = self._finalize_unknown_skill_resolution(unknown_skill, None)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role="HR_ADMIN",
            action_type=ActionType.UNKNOWN_SKILL_DELETED,
            entity_type=EntityType.UNKNOWN_SKILL,
            entity_id=unknown_skill_id,
            jurisdiction=None,
            details={"raw_text": raw_text, **counts},
        )
        return {
            "id": unknown_skill_id,
            "raw_text": raw_text,
            "jd_unknown_skills_deleted": counts["jd_unknown_skills_deleted"],
            "candidate_skills_deleted": counts["candidate_skills_deleted"],
        }

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
                    # Same flat weight the JD parsing pipeline assigns
                    # (M07) - this is a retroactive jd_skills row for an
                    # existing JD, so it must never be created NULL either.
                    weight=_DEFAULT_JD_SKILL_WEIGHT,
                )
                self.skill_repository.bump_occurrence_count(canonical_skill_id)
            self.skill_repository.mark_jd_unknown_skill_resolved(link)

    def _finalize_unknown_skill_resolution(
        self, unknown_skill: UnknownSkill, canonical_skill_id: UUID | None
    ) -> dict:
        """
        Common tail shared by every "resolve this UnknownSkill for good"
        flow (create-canonical, bulk approve, delete, bulk delete): when
        canonical_skill_id is given, migrates every JD/candidate occurrence
        still linked to unknown_skill onto it as a verified (MANUAL_HR)
        match first; either way, hard-deletes the UnknownSkill row (and
        whatever JDUnknownSkill/CandidateSkill links remain) and recomputes
        is_verified for every JD that was touched.
        """
        jd_skills_migrated = 0
        candidate_skills_migrated = 0

        if canonical_skill_id is not None:
            jd_links = self.skill_repository.get_pending_jd_links(unknown_skill.id)
            jd_ids = {link.jd_id for link in jd_links}
            self._create_retroactive_jd_skills(unknown_skill, canonical_skill_id)
            jd_skills_migrated = len(jd_links)
            for link in jd_links:
                self.skill_repository.delete_jd_unknown_skill(link)

            for candidate_skill in self.skill_repository.get_candidate_skills_by_unknown_skill_id(unknown_skill.id):
                if not self.skill_repository.get_candidate_skill_by_canonical(
                    candidate_skill.resume_id, canonical_skill_id
                ):
                    self.resume_repository.create_candidate_skill(
                        candidate_id=candidate_skill.candidate_id,
                        resume_id=candidate_skill.resume_id,
                        canonical_skill_id=canonical_skill_id,
                        raw_extracted_text=candidate_skill.raw_extracted_text,
                        confidence=1.0,
                        match_tier=SkillMatchTier.MANUAL_HR.value,
                        status=verification_status_for_tier(SkillMatchTier.MANUAL_HR).value,
                        scoring_weight=scoring_weight_for_tier(SkillMatchTier.MANUAL_HR),
                    )
                    self.skill_repository.bump_occurrence_count(canonical_skill_id)
                    candidate_skills_migrated += 1
                self.skill_repository.delete_candidate_skill(candidate_skill)
        else:
            # Pure delete: no migration target, so gather every linked
            # jd_id (pending or already resolved) rather than just pending
            # ones, since the cascade below wipes both kinds of link.
            jd_ids = {
                jd.id for _link, jd in self.skill_repository.get_jd_links_by_unknown_skill_id(unknown_skill.id)
            }

        delete_counts = self.skill_repository.delete_unknown_skill_cascade(unknown_skill)
        self._refresh_jd_verification_status(jd_ids)

        return {
            "jd_skills_migrated": jd_skills_migrated,
            "candidate_skills_migrated": candidate_skills_migrated,
            **delete_counts,
        }

    def _enqueue_skill_embedding(self, skill_id: UUID) -> None:
        # Fire-and-forget: the caller's mutation has already committed, so a
        # broker outage here must never undo it or fail the request - only
        # logged. The Missing Skill Embedding Recovery utility picks up
        # anything left un-queued.
        try:
            self.embedding_queue_service.queue_skill_embedding(skill_id)
        except EmbeddingQueueError:
            logger.exception("Failed to enqueue embedding generation for skill '%s'.", skill_id)

    def _append_alias_validated(self, skill: SkillOntology, alias: str, actor_id: str) -> None:
        # Acquire before validating: aliases have no DB uniqueness
        # constraint, so two concurrent "add this alias" calls for
        # different skills could otherwise both pass the collision check
        # before either commits. Held for the rest of this transaction.
        self.skill_repository.acquire_alias_lock(alias)

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

    @staticmethod
    def _clean_aliases(aliases: list[str] | None) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for alias in aliases or []:
            trimmed = alias.strip()
            if not trimmed or trimmed in seen:
                continue
            seen.add(trimmed)
            cleaned.append(trimmed)
        return cleaned

    def _refresh_jd_verification_status(self, jd_ids: set[UUID]) -> None:
        """
        Recomputes is_verified for every JD that was touched by an unknown-
        skill resolution action - cheap (indexed COUNT + at most a 1-row
        UPDATE per JD), so it runs inline in the same transaction rather
        than being pushed to a background task. Called before commit() so
        the flip to VERIFIED lands atomically with the rest of the action.
        """
        for jd_id in jd_ids:
            self.skill_repository.mark_jd_verified_if_fully_resolved(jd_id)

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
