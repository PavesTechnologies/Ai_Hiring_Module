"""
Resolving a JD's unknown skills from the Calibration tab (Phase 2 Step 2).

  MAP  - this JD only: the skill becomes a jd_skills row for the chosen
         catalogue skill (tier carried over, HR_VERIFIED by the HR user).
         Other JDs with the same text are untouched.
  NEW  - global, as the catalogue is: the text becomes a new catalogue skill
         and every JD still waiting on it is resolved (the existing
         create-canonical path, incl. the resume-side re-evaluation).
  DROP - this JD only: no jd_skills row; the JD stops scoring it.

Every JD a decision resolves gets the same follow-up (jd_skill_change_effects):
is_verified recomputed, a calibration version if it's calibrated, its live
candidates marked out of date. Suggestions: the top 3 catalogue skills by
embedding similarity. Nothing here sends email.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.skills import (
    JDSkillImportance, JDSkillVerificationStatus, JDUnknownSkill, JDUnknownSkillResolution, JDUnknownSkillStatus,
    UnknownSkill,
)
from app.schemas.jd.calibration import (
    JDUnknownSkillResolveRequest, JDUnknownSkillResolveResponse, SkillSuggestion, UnknownSkillSuggestions,
)
from app.services.jd.jd_skill_change_effects import apply_jd_skill_change_effects
from app.services.jd.jd_skill_importance import unknown_skill_tier
from app.services.skills.skill_normalization_service import SkillMatchTier

logger = logging.getLogger(__name__)

SUGGESTION_COUNT = 3
_JD_SKILL_WEIGHT = 1.0


class JDUnknownSkillService:
    def __init__(self, db, skill_repository, curation_service, calibration_service, audit_service,
                 embedding_service=None):
        self.db = db
        self.skill_repository = skill_repository
        self.curation_service = curation_service
        self.calibration_service = calibration_service
        self.audit_service = audit_service
        self.embedding_service = embedding_service

    # --- suggestions ----------------------------------------------------------
    def suggestions(self, jd_id: UUID) -> list[UnknownSkillSuggestions]:
        jd = self.calibration_service._jd(jd_id)
        result = []
        for link, unknown in self.calibration_service.repo.unknown_skill_links(jd.id):
            if link.status == JDUnknownSkillStatus.PENDING:
                result.append(UnknownSkillSuggestions(unknown_skill_id=unknown.id,
                                                      suggestions=self._top_skills(unknown.raw_text)))
        return result

    def _top_skills(self, raw_text: str) -> list[SkillSuggestion]:
        if self.embedding_service is None:
            return []
        try:
            embedding = self.embedding_service.generate_embedding(raw_text)
            nearest = self.skill_repository.find_top_similar_canonical_skills(embedding, limit=SUGGESTION_COUNT)
        except Exception:
            logger.exception("Unknown-skill suggestions failed | raw_text=%r", raw_text)
            return []
        return [SkillSuggestion(canonical_skill_id=skill.id, canonical_name=skill.canonical_name,
                                similarity=round(float(similarity), 4)) for skill, similarity in nearest]

    # --- resolve --------------------------------------------------------------
    def resolve(self, jd_id: UUID, unknown_skill_id: UUID, request: JDUnknownSkillResolveRequest, *,
                actor_id: str, actor_role: str) -> JDUnknownSkillResolveResponse:
        jd = self.calibration_service._jd(jd_id)
        link, unknown = self._pending_link(jd.id, unknown_skill_id)
        if request.action == "NEW":
            return self._approve_new(jd, unknown, request, actor_id=actor_id, actor_role=actor_role)
        try:
            canonical_skill_id, created = None, False
            now = datetime.now(timezone.utc)
            if request.action == "MAP":
                canonical_skill_id, created = self._map(jd, link, unknown, request, actor_id=actor_id, now=now)
                note = f"unknown skill '{unknown.raw_text}' mapped to '{self._skill_name(canonical_skill_id)}'"
                link.resolution, link.resolved_canonical_skill_id = JDUnknownSkillResolution.MAPPED, canonical_skill_id
            else:
                note = f"unknown skill '{unknown.raw_text}' dropped"
                link.resolution = JDUnknownSkillResolution.DROPPED
            link.status, link.resolved_by, link.resolved_at = JDUnknownSkillStatus.RESOLVED, actor_id, now
            self.db.flush()
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.JD_UNKNOWN_SKILL_RESOLVED,
                entity_type=EntityType.JOB_DESCRIPTION, entity_id=jd.id,
                details={"unknown_skill_id": str(unknown.id), "raw_text": unknown.raw_text,
                         "action": request.action,
                         "canonical_skill_id": str(canonical_skill_id) if canonical_skill_id else None},
            )
            effects = apply_jd_skill_change_effects(
                self.db, [jd.id], actor_id=actor_id, actor_role=actor_role, note=note,
                audit_service=self.audit_service, skill_repository=self.skill_repository,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.curation_service.on_committed({jd.id} if created else set(), stale_jd_ids={jd.id})
        return JDUnknownSkillResolveResponse(
            action=request.action, unknown_skill_id=unknown.id, canonical_skill_id=canonical_skill_id,
            affected_jd_ids=[jd.id],
            candidates_marked_out_of_date=sum(e["candidates_marked_out_of_date"] for e in effects.values()),
            calibration=self.calibration_service.get_view(jd.id, can_calibrate=True),
        )

    def _map(self, jd, link, unknown, request, *, actor_id: str, now) -> tuple[UUID, bool]:
        if request.canonical_skill_id is None:
            raise UnprocessableError("Choose the catalogue skill to map this to.")
        target = self.skill_repository.get_skill_by_id(request.canonical_skill_id)
        if target is None or not getattr(target, "is_active", True):
            raise NotFoundError("Catalogue skill not found.")
        if self.skill_repository.get_jd_skill(jd.id, target.id):
            return target.id, False             # the JD already has it - the unknown text just resolves to it
        tier = unknown_skill_tier(jd.extracted_json, link, unknown)
        self.skill_repository.create_jd_skill(
            jd_id=jd.id, canonical_skill_id=target.id, mandatory=tier != "PREFERRED",
            match_tier=SkillMatchTier.MANUAL_HR.value, verification_status=JDSkillVerificationStatus.HR_VERIFIED,
            confidence=1.0, weight=_JD_SKILL_WEIGHT,
            importance=JDSkillImportance(tier) if tier in ("CORE", "SUPPORTING") else None,
            verified_by=actor_id, verified_at=now,
        )
        self.skill_repository.bump_occurrence_count(target.id)
        return target.id, True

    def _approve_new(self, jd, unknown, request, *, actor_id: str, actor_role: str) -> JDUnknownSkillResolveResponse:
        """Global: the existing create-canonical path resolves every JD with this text (and commits)."""
        affected = [link.jd_id for link in self.skill_repository.get_pending_jd_links(unknown.id)]
        self.curation_service.last_change_effects = {}
        result = self.curation_service.create_canonical_skill_from_unknown(
            unknown.id, actor_id, canonical_name=(request.canonical_name or unknown.raw_text).strip(),
            aliases=request.aliases, category=request.category, parent_skill_id=request.parent_skill_id,
        )
        effects = getattr(self.curation_service, "last_change_effects", {}) or {}
        return JDUnknownSkillResolveResponse(
            action="NEW", unknown_skill_id=unknown.id, canonical_skill_id=result["skill"].id,
            affected_jd_ids=affected,
            candidates_marked_out_of_date=sum(e["candidates_marked_out_of_date"] for e in effects.values()),
            calibration=self.calibration_service.get_view(jd.id, can_calibrate=True),
        )

    # --- helpers --------------------------------------------------------------
    def _pending_link(self, jd_id: UUID, unknown_skill_id: UUID) -> tuple[JDUnknownSkill, UnknownSkill]:
        row = (self.db.query(JDUnknownSkill, UnknownSkill)
               .join(UnknownSkill, UnknownSkill.id == JDUnknownSkill.unknown_skill_id)
               .filter(JDUnknownSkill.jd_id == jd_id, JDUnknownSkill.unknown_skill_id == unknown_skill_id)
               .first())
        if row is None:
            raise NotFoundError("This JD has no such unknown skill.")
        link, unknown = row
        if link.status != JDUnknownSkillStatus.PENDING:
            raise ConflictError("This unknown skill is already resolved for this JD.")
        return link, unknown

    def _skill_name(self, skill_id: UUID) -> str:
        skill = self.skill_repository.get_skill_by_id(skill_id)
        return getattr(skill, "canonical_name", str(skill_id))
