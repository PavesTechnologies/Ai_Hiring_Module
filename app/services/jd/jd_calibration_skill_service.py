"""
Adding and removing JD skills in calibration (HR_ADMIN).

  add    - a catalogue skill, or a new one (added to the catalogue), with a
           tier and optional meaning. The jd_skills row is recorded as an HR
           decision: HR_VERIFIED, verified_by / verified_at, match tier
           MANUAL_HR.
  remove - e.g. a skill the extraction invented. Only the JD's link goes;
           the catalogue skill stays for every other JD and resume.

Allowed while a campaign using the JD is live (calibration may change then,
D2). Each change gets the shared follow-up (jd_skill_change_effects): a
calibration version on a calibrated JD (its changes list the added / removed
skill), verification recomputed, live candidates marked out of date. Each is
also audited (JD_SKILL_UPDATED "added in calibration" / JD_SKILL_REMOVED).
Nothing here sends email.
"""
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.skills import JDSkill, JDSkillImportance, JDSkillVerificationStatus
from app.schemas.jd.calibration import JDCalibrationAddSkillRequest, JDCalibrationSkillChangeResponse
from app.services.jd.jd_skill_change_effects import apply_jd_skill_change_effects
from app.services.skills.skill_normalization_service import SkillMatchTier

_JD_SKILL_WEIGHT = 1.0
NEW_SKILL_SOURCE = "HR_CALIBRATION"


class JDCalibrationSkillService:
    def __init__(self, db, skill_repository, curation_service, calibration_service, audit_service):
        self.db = db
        self.skill_repository = skill_repository
        self.curation_service = curation_service
        self.calibration_service = calibration_service
        self.audit_service = audit_service

    def add(self, jd_id: UUID, request: JDCalibrationAddSkillRequest, *, actor_id: str,
            actor_role: str) -> JDCalibrationSkillChangeResponse:
        jd = self.calibration_service._jd(jd_id)
        created_new = False
        try:
            skill = self._target_skill(request)
            if skill is None:                                    # a new catalogue skill
                skill = self.skill_repository.create_skill_ontology(
                    canonical_name=request.new_skill_name.strip(), source=NEW_SKILL_SOURCE)
                created_new = True
            if self.skill_repository.get_jd_skill(jd.id, skill.id):
                raise ConflictError(f"'{skill.canonical_name}' is already on this JD - change its tier instead.")
            self._check_sense(skill.id, request.sense_key)
            now = datetime.now(timezone.utc)
            row = self.skill_repository.create_jd_skill(
                jd_id=jd.id, canonical_skill_id=skill.id, mandatory=request.tier != "PREFERRED",
                match_tier=SkillMatchTier.MANUAL_HR.value, verification_status=JDSkillVerificationStatus.HR_VERIFIED,
                confidence=1.0, weight=_JD_SKILL_WEIGHT,
                importance=JDSkillImportance(request.tier) if request.tier in ("CORE", "SUPPORTING") else None,
                verified_by=actor_id, verified_at=now,
            )
            row.sense_key = request.sense_key
            self.skill_repository.bump_occurrence_count(skill.id)
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.JD_SKILL_UPDATED,
                entity_type=EntityType.JD_SKILL, entity_id=row.id,
                details={"change": "added in calibration", "jd_id": str(jd.id), "canonical_skill_id": str(skill.id),
                         "skill": skill.canonical_name, "tier": request.tier, "sense_key": request.sense_key,
                         "new_catalogue_skill": created_new},
            )
            note = (f"'{skill.canonical_name}' added as {request.tier.lower()}"
                    + (" (new catalogue skill)" if created_new else ""))
            effects = apply_jd_skill_change_effects(self.db, [jd.id], actor_id=actor_id, actor_role=actor_role,
                                                    note=note, audit_service=self.audit_service,
                                                    skill_repository=self.skill_repository)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if created_new:
            self.curation_service.enqueue_skill_embedding(skill.id)
        self.curation_service.on_committed({jd.id}, stale_jd_ids={jd.id}, catalog_changed=created_new)
        return self._response("ADDED", jd.id, row.id, skill, created_new, effects)

    def remove(self, jd_id: UUID, jd_skill_id: UUID, *, actor_id: str, actor_role: str) -> JDCalibrationSkillChangeResponse:
        jd = self.calibration_service._jd(jd_id)
        try:
            row = self.db.get(JDSkill, jd_skill_id)
            if row is None or row.jd_id != jd.id:
                raise NotFoundError("This JD has no such skill.")
            if row.importance == JDSkillImportance.CORE and row.mandatory \
                    and self.skill_repository.count_core_jd_skills(jd.id) <= 1:
                raise UnprocessableError("This is the JD's only core skill. Make another skill core before removing it.")
            skill = self.skill_repository.get_skill_by_id(row.canonical_skill_id)
            name = getattr(skill, "canonical_name", str(row.canonical_skill_id))
            details = {"change": "removed in calibration", "jd_id": str(jd.id),
                       "canonical_skill_id": str(row.canonical_skill_id), "skill": name,
                       "mandatory": row.mandatory, "importance": getattr(row.importance, "value", row.importance),
                       "match_tier": row.match_tier}
            self.skill_repository.delete_jd_skill(row)
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.JD_SKILL_REMOVED,
                entity_type=EntityType.JD_SKILL, entity_id=jd_skill_id, details=details,
            )
            effects = apply_jd_skill_change_effects(self.db, [jd.id], actor_id=actor_id, actor_role=actor_role,
                                                    note=f"'{name}' removed", audit_service=self.audit_service,
                                                    skill_repository=self.skill_repository)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.curation_service.on_committed({jd.id}, stale_jd_ids={jd.id})
        return self._response("REMOVED", jd.id, jd_skill_id, skill, False, effects)

    # --- helpers --------------------------------------------------------------
    def _target_skill(self, request: JDCalibrationAddSkillRequest):
        if request.canonical_skill_id is not None:
            skill = self.skill_repository.get_skill_by_id(request.canonical_skill_id)
            if skill is None or not getattr(skill, "is_active", True):
                raise NotFoundError("Catalogue skill not found.")
            return skill
        existing = self.skill_repository.find_skill_by_name_or_alias(request.new_skill_name.strip())
        if existing is not None:
            raise ConflictError(f"'{request.new_skill_name.strip()}' is already in the catalogue as "
                                f"'{existing.canonical_name}' - add it from the catalogue instead.")
        return None

    def _check_sense(self, skill_id: UUID, sense_key: str | None) -> None:
        if sense_key is None:
            return
        senses = {s.sense_key for s in self.calibration_service.repo.load_senses() if s.skill_id == skill_id}
        if sense_key not in senses:
            raise UnprocessableError("That meaning doesn't belong to this skill.")

    def _response(self, action, jd_id, jd_skill_id, skill, created_new, effects) -> JDCalibrationSkillChangeResponse:
        effect = effects.get(jd_id, {})
        return JDCalibrationSkillChangeResponse(
            action=action, jd_skill_id=jd_skill_id, canonical_skill_id=getattr(skill, "id", None),
            skill=getattr(skill, "canonical_name", None), created_new_skill=created_new,
            calibration_version=effect.get("calibration_version"),
            candidates_marked_out_of_date=effect.get("candidates_marked_out_of_date", 0),
            calibration=self.calibration_service.get_view(jd_id, can_calibrate=True),
        )
