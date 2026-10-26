"""
JD calibration (Phase 2 Step 1): HR_ADMIN reviews and confirms a JD's scoring
inputs - core / supporting / preferred skills (a warning above 5 core skills,
never a block), the sense of every ambiguous skill (no "assumed" senses on a
calibrated JD), the minimum experience and its near-miss tolerance (default
max(1 year, 10% of the minimum)), and education mandatory or preferred.

Saving moves the JD to CALIBRATED with who/when, and writes an immutable
version (1, 2, ...) whenever something changed. Live campaigns (ACTIVE /
PAUSED) on the JD keep their scores: their scored candidates are marked
"score out of date" and re-scored under the existing rules. Calibrating also
ends any exemption the JD's campaigns had from the activation rule.
"""
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.models.jd.job_descriptions import (
    JDCalibrationStatus,
    JDCalibrationVersion,
    JDEducationRequirement,
)
from app.models.skills import JDSkillImportance, JDSkillVerificationStatus
from app.repositories.jd_calibration_repository import JDCalibrationRepository
from app.schemas.jd.calibration import (
    CalibrationCampaign,
    CalibrationSkill,
    CalibrationUnknownSkill,
    JDCalibrationSaveRequest,
    JDCalibrationSaveResponse,
    JDCalibrationVersionItem,
    JDCalibrationView,
    SenseOption,
)
from app.services.audit_service import AuditService
from app.services.campaign.near_miss import experience_near_miss_tolerance
from app.services.campaign.score_out_of_date import LIVE_CAMPAIGN_STATUSES, mark_campaigns_out_of_date
from app.repositories.jd_profile_template_repository import JDProfileTemplateRepository
from app.schemas.jd_profile_template import JDProfileTemplateItem
from app.services.jd.jd_calibration_rules import (
    calibrated_min_experience, default_internship_credit_pct, is_fresher_jd, jd_profile, jd_role_domain,
)
from app.services.jd.jd_profile_templates import suggest_profile_template
from app.services.jd.jd_requirement_text import suggest_requirement_text
from app.services.jd.jd_role_domain import suggest_role_domain
from app.services.jd.jd_skill_importance import unknown_skill_tier
from app.services.skills.skill_sense_resolver import SkillSenseResolver

CORE_SKILL_WARNING_THRESHOLD = 5


def _tier(mandatory: bool, importance) -> str | None:
    if not mandatory:
        return "PREFERRED"
    value = getattr(importance, "value", importance)
    return value if value in ("CORE", "SUPPORTING") else None


def _float(value) -> float | None:
    return float(value) if value is not None else None


def effective_min_experience(jd) -> float | None:
    """
    The minimum scoring uses: the calibrated value on a calibrated JD, else the
    extraction's value, else the column (as in validate_experience).
    """
    calibrated = calibrated_min_experience(jd)
    return calibrated if calibrated is not None else extracted_min_experience(jd)


def extracted_min_experience(jd) -> float | None:
    """The extraction's minimum, else the column - never changed by calibration."""
    extracted = ((jd.extracted_json or {}).get("experience") or {}).get("min_experience_years")
    return _float(extracted) if extracted is not None else _float(jd.min_experience_years)


class JDCalibrationService:
    def __init__(self, repo: JDCalibrationRepository, audit_service: AuditService):
        self.repo = repo
        self.audit_service = audit_service

    # --- read ---------------------------------------------------------------
    def get_view(self, jd_id: UUID, *, can_calibrate: bool, warnings: list[str] | None = None) -> JDCalibrationView:
        jd = self._jd(jd_id)
        resolver = SkillSenseResolver(self.repo.load_senses())
        title_text = " ".join(str(v) for v in (jd.title, (jd.extracted_json or {}).get("domain")) if v)

        skills = []
        jd_rows = self.repo.jd_skills_with_names(jd.id)
        aliases = self.repo.skill_aliases([row.canonical_skill_id for row, _ in jd_rows])
        for row, name in jd_rows:
            options = resolver.senses_for(row.canonical_skill_id)
            ambiguous = resolver.has_senses(row.canonical_skill_id)
            suggestion = resolver.resolve_jd_sense(row.canonical_skill_id, jd.raw_text or "", name, title_text)
            skills.append(CalibrationSkill(
                jd_skill_id=row.id, canonical_skill_id=row.canonical_skill_id, name=name,
                tier=_tier(row.mandatory, row.importance), ambiguous=ambiguous,
                sense_options=[SenseOption(sense_key=o.sense_key, label=o.label, description=o.description,
                                           is_default=o.is_default) for o in options] if ambiguous else [],
                sense_key=row.sense_key if ambiguous else None,
                suggested_sense_key=suggestion.sense if ambiguous else None,
                suggested_sense_basis=suggestion.basis if ambiguous else None,
                required=_tier(row.mandatory, row.importance) == "CORE" and getattr(row, "required", False) is True,
                requirement_text=getattr(row, "requirement_text", None),
                suggested_requirement_text=suggest_requirement_text(
                    jd.raw_text, name, aliases.get(row.canonical_skill_id, [])),
            ))

        unknown = self._unknown_skills(jd)
        pending = sum(1 for u in unknown if u.status == "PENDING")
        blocking = [u.raw_text for u in unknown if u.blocks_calibration]

        draft_from, dropped = self._draft_origin(jd, jd_rows)
        # A draft shows the carried calibrated minimum (scoring reads it only once calibrated).
        draft_min = _float(jd.calibrated_min_experience_years) if draft_from is not None else None
        min_years = draft_min if draft_min is not None else effective_min_experience(jd)
        campaigns = self.repo.campaigns_for_jd(jd.id)
        live = [c for c in campaigns if c.status in LIVE_CAMPAIGN_STATUSES]
        names = self.repo.user_names([jd.calibrated_by]) if jd.calibrated_by else {}
        core_count = sum(1 for s in skills if s.tier == "CORE")
        domain_suggestion = suggest_role_domain(jd.title, [s.name for s in skills if s.tier == "CORE"])
        template_suggestion = suggest_profile_template(
            title=jd.title, min_years=min_years, role_domain=jd_role_domain(jd) or domain_suggestion.domain,
            certification_count=len((jd.extracted_json or {}).get("certifications") or []),
        )
        profile = jd_profile(jd)
        template_tolerance = getattr(profile, "experience_tolerance_years", None)

        view_warnings = list(warnings or [])
        view_warnings += self._skill_warnings(core_count, [s for s in skills if s.ambiguous and not s.sense_key])
        if blocking:
            view_warnings.append("Resolve these core skills before calibrating (map, approve as new or drop): "
                                 + ", ".join(blocking) + ".")
        if pending:
            view_warnings.append(f"{pending} skill(s) from this JD are not in the catalogue yet (unknown skills); "
                                 f"until resolved they're scored provisionally (text match at 0.8), never as a gate.")

        return JDCalibrationView(
            jd_id=jd.id, title=jd.title, version_number=jd.version_number,
            calibration_status=jd.calibration_status.value, calibration_version=jd.calibration_version,
            calibrated_by=jd.calibrated_by, calibrated_by_name=names.get(jd.calibrated_by),
            calibrated_at=jd.calibrated_at,
            verification_status=jd.is_verified.value, pending_unknown_skill_count=pending,
            skills=skills, unknown_skills=unknown, core_skill_count=core_count,
            min_experience_years=min_years,
            extracted_min_experience_years=extracted_min_experience(jd),
            experience_tolerance_years=_float(jd.experience_tolerance_years),
            # What a blank tolerance means: the template's, else max(1 year, 10% of the minimum).
            default_experience_tolerance_years=(
                float(template_tolerance) if template_tolerance is not None
                else experience_near_miss_tolerance(min_years) if min_years is not None else None),
            education_requirement=jd.education_requirement.value,
            education_degree=(jd.education_criteria or {}).get("degree"),
            education_field=(jd.education_criteria or {}).get("field"),
            internship_credit_pct=jd.internship_credit_pct,
            default_internship_credit_pct=default_internship_credit_pct(jd),
            is_fresher_jd=is_fresher_jd(jd),
            role_domain=jd_role_domain(jd),
            suggested_role_domain=domain_suggestion.domain,
            suggested_role_domain_basis=domain_suggestion.basis,
            profile_template=jd.profile_template if profile is not None else None,
            suggested_profile_template=template_suggestion.key,
            suggested_profile_template_basis=template_suggestion.basis,
            profile_templates=[JDProfileTemplateItem.model_validate(t)
                               for t in JDProfileTemplateRepository(self.repo.db).list_all()],
            live_campaigns=[CalibrationCampaign(campaign_id=c.id, name=c.name, status=c.status.value) for c in live],
            can_calibrate=can_calibrate,
            blocking_unknown_skills=blocking,
            calibration_draft_from_version=draft_from.version_number if draft_from is not None else None,
            calibration_draft_dropped_skills=dropped,
            warnings=list(dict.fromkeys(view_warnings)),
        )

    def _draft_origin(self, jd, jd_rows: list) -> tuple:
        """
        An uncalibrated version carrying an earlier version's calibration: that version,
        and the names of its skills this version no longer has (new text dropped them).
        """
        draft_from_id = getattr(jd, "calibration_draft_from", None)
        if draft_from_id is None or jd.calibration_status == JDCalibrationStatus.CALIBRATED:
            return None, []
        previous = self.repo.get_jd(draft_from_id)
        if previous is None:
            return None, []
        current = {row.canonical_skill_id for row, _ in jd_rows}
        dropped = [name for row, name in self.repo.jd_skills_with_names(previous.id)
                   if row.canonical_skill_id not in current]
        return previous, dropped

    def list_versions(self, jd_id: UUID) -> list[JDCalibrationVersionItem]:
        self._jd(jd_id)
        versions = self.repo.list_versions(jd_id)
        names = self.repo.user_names([v.calibrated_by for v in versions])
        return [JDCalibrationVersionItem(version_no=v.version_no, calibrated_by=v.calibrated_by,
                                         calibrated_by_name=names.get(v.calibrated_by),
                                         calibrated_at=v.calibrated_at, changes=v.changes) for v in versions]

    # --- save ---------------------------------------------------------------
    def save(self, jd_id: UUID, request: JDCalibrationSaveRequest, *, actor_id: str,
             actor_role: str | None) -> JDCalibrationSaveResponse:
        try:
            jd = self._jd(jd_id)
            blocking = [u.raw_text for u in self._unknown_skills(jd) if u.blocks_calibration]
            if blocking:
                raise UnprocessableError("Resolve these core skills before calibrating (map, approve as new or drop): "
                                         + ", ".join(blocking) + ".")
            rows = {row.id: (row, name) for row, name in self.repo.jd_skills_with_names(jd.id)}
            resolver = SkillSenseResolver(self.repo.load_senses())
            new_skills = self._validated_skills(request, rows, resolver)
            min_years = request.min_experience_years
            education = request.education_requirement
            template = (JDProfileTemplateRepository(self.repo.db).get(request.profile_template)
                        if request.profile_template else None)
            if request.profile_template and template is None:
                raise UnprocessableError(f"Unknown profile template: {request.profile_template}.")

            before = self._snapshot(jd, {sid: (row, name) for sid, (row, name) in rows.items()})
            after = {
                "skills": {str(sid): {**before["skills"][str(sid)], **value} for sid, value in new_skills.items()},
                "min_experience_years": min_years,
                "experience_tolerance_years": request.experience_tolerance_years,
                "education_requirement": education,
                "internship_credit_pct": request.internship_credit_pct,
                "role_domain": request.role_domain,
                "profile_template": request.profile_template,
            }
            changes = self._changes(before, after)
            already_calibrated = jd.calibration_status == JDCalibrationStatus.CALIBRATED
            core_count = sum(1 for v in new_skills.values() if v["tier"] == "CORE")
            warnings = self._skill_warnings(core_count, [])

            if already_calibrated and not changes:
                return JDCalibrationSaveResponse(
                    changed=False, calibration_version=jd.calibration_version, candidates_marked_out_of_date=0,
                    warnings=warnings, calibration=self.get_view(jd.id, can_calibrate=True, warnings=warnings))

            now = datetime.now(timezone.utc)
            for sid, value in new_skills.items():
                row, _ = rows[sid]
                row.mandatory = value["tier"] != "PREFERRED"
                row.importance = JDSkillImportance(value["tier"]) if value["tier"] in ("CORE", "SUPPORTING") else None
                row.sense_key = value["sense_key"]
                row.required = value["required"]
                row.requirement_text = value["requirement_text"]
                row.verification_status = JDSkillVerificationStatus.HR_VERIFIED   # confirmed by HR in calibration
                row.verified_by = actor_id
                row.verified_at = now

            # The extraction and min_experience_years stay as extracted; scoring reads this first.
            jd.calibrated_min_experience_years = min_years
            jd.experience_tolerance_years = request.experience_tolerance_years
            jd.education_requirement = JDEducationRequirement(education)
            jd.internship_credit_pct = request.internship_credit_pct
            jd.role_domain = request.role_domain
            # Both sides, so scoring in this same session reads the new template.
            jd.profile = template
            jd.profile_template = request.profile_template
            jd.calibration_status = JDCalibrationStatus.CALIBRATED
            jd.calibrated_by = actor_id
            jd.calibrated_at = now
            jd.calibration_version = self.repo.latest_version_no(jd.id) + 1
            jd.updated_at = now
            self.repo.add_version(JDCalibrationVersion(
                jd_id=jd.id, version_no=jd.calibration_version, snapshot=after,
                changes=changes or None, calibrated_by=actor_id, calibrated_at=now,
            ))

            campaigns = self.repo.campaigns_for_jd(jd.id)
            for campaign in campaigns:
                campaign.jd_calibration_exempt = False        # the exemption ends once the JD is calibrated
            live_ids = [c.id for c in campaigns if c.status in LIVE_CAMPAIGN_STATUSES]
            marked = 0
            if live_ids and changes:
                self.repo.flush()
                marked = mark_campaigns_out_of_date(self.repo.db, live_ids, (
                    f"The JD '{jd.title}' calibration changed (version {jd.calibration_version}) on "
                    f"{now:%Y-%m-%d}: these scores were computed with the previous settings."
                ))

            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role,
                action_type=(ActionType.JD_CALIBRATION_CHANGED if already_calibrated else ActionType.JD_CALIBRATED),
                entity_type=EntityType.JOB_DESCRIPTION, entity_id=jd.id,
                details={"calibration_version": jd.calibration_version, "changes": changes,
                         "live_campaign_ids": [str(i) for i in live_ids],
                         "candidates_marked_out_of_date": marked},
            )
            self.repo.commit()
        except Exception:
            self.repo.rollback()
            raise
        return JDCalibrationSaveResponse(
            changed=True, calibration_version=jd.calibration_version, candidates_marked_out_of_date=marked,
            warnings=warnings, calibration=self.get_view(jd.id, can_calibrate=True, warnings=warnings))

    def record_skill_change(self, jd_id: UUID, *, actor_id: str, actor_role: str, note: str) -> int | None:
        """
        A calibrated JD's skills changed outside the Calibration tab (an
        unknown skill mapped / approved as new / dropped): save the new state
        as the next calibration version, diffed against the last one, and
        audit it. Not committed - part of the caller's transaction. Returns
        the new version number, or None when the JD isn't calibrated.
        """
        jd = self.repo.get_jd(jd_id)
        if jd is None or jd.calibration_status != JDCalibrationStatus.CALIBRATED:
            return None
        self.repo.flush()
        after = self._snapshot(jd, {row.id: (row, name) for row, name in self.repo.jd_skills_with_names(jd.id)})
        latest = self.repo.latest_version(jd.id)
        before = latest.snapshot if latest is not None else {**after, "skills": {}}
        changes = {**self._changes(before, after), "note": note}
        now = datetime.now(timezone.utc)
        jd.calibration_version = self.repo.latest_version_no(jd.id) + 1
        jd.updated_at = now
        self.repo.add_version(JDCalibrationVersion(
            jd_id=jd.id, version_no=jd.calibration_version, snapshot=after, changes=changes,
            calibrated_by=actor_id, calibrated_at=now,
        ))
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.JD_CALIBRATION_CHANGED,
            entity_type=EntityType.JOB_DESCRIPTION, entity_id=jd.id,
            details={"calibration_version": jd.calibration_version, "changes": changes},
        )
        return jd.calibration_version

    # --- helpers ------------------------------------------------------------
    def _unknown_skills(self, jd) -> list[CalibrationUnknownSkill]:
        links = self.repo.unknown_skill_links(jd.id)
        canonical = self.repo.skill_names([link.resolved_canonical_skill_id for link, _ in links
                                           if getattr(link, "resolved_canonical_skill_id", None)])
        resolvers = self.repo.user_names([link.resolved_by for link, _ in links if getattr(link, "resolved_by", None)])
        result = []
        for link, unknown_skill in links:
            tier = unknown_skill_tier(jd.extracted_json, link, unknown_skill)
            status = getattr(link.status, "value", str(link.status))
            resolution = getattr(link, "resolution", None)
            resolved_id = getattr(link, "resolved_canonical_skill_id", None)
            result.append(CalibrationUnknownSkill(
                unknown_skill_id=unknown_skill.id, raw_text=unknown_skill.raw_text, tier=tier, status=status,
                resolution=getattr(resolution, "value", resolution),
                resolved_canonical_skill_id=resolved_id, resolved_canonical_name=canonical.get(resolved_id),
                resolved_by_name=resolvers.get(getattr(link, "resolved_by", None)),
                resolved_at=getattr(link, "resolved_at", None),
                blocks_calibration=status == "PENDING" and tier == "CORE",
            ))
        return result

    def _jd(self, jd_id: UUID):
        jd = self.repo.get_jd(jd_id)
        if jd is None:
            raise NotFoundError("Job description not found.")
        return jd

    @staticmethod
    def _validated_skills(request: JDCalibrationSaveRequest, rows: dict, resolver: SkillSenseResolver) -> dict:
        ids = [s.jd_skill_id for s in request.skills]
        if len(ids) != len(set(ids)):
            raise UnprocessableError("Each JD skill can appear only once.")
        unknown_ids = set(ids) - set(rows)
        missing = set(rows) - set(ids)
        if unknown_ids:
            raise UnprocessableError("Some skills don't belong to this JD.")
        if missing:
            names = ", ".join(sorted(rows[sid][1] for sid in missing))
            raise UnprocessableError(f"Every JD skill must be confirmed; missing: {names}.")
        if not any(s.tier == "CORE" for s in request.skills):
            raise UnprocessableError("A calibrated JD needs at least one core skill.")
        not_core = [rows[s.jd_skill_id][1] for s in request.skills if s.required and s.tier != "CORE"]
        if not_core:
            raise UnprocessableError("Only a core skill can be required: " + ", ".join(sorted(not_core)) + ".")
        result = {}
        unset = []
        for item in request.skills:
            row, name = rows[item.jd_skill_id]
            sense_key = None
            if resolver.has_senses(row.canonical_skill_id):
                valid = {o.sense_key for o in resolver.senses_for(row.canonical_skill_id)}
                if item.sense_key not in valid:
                    unset.append(name)
                sense_key = item.sense_key
            # Only a core skill keeps a requirement text; blank = none.
            requirement_text = ((item.requirement_text or "").strip() or None) if item.tier == "CORE" else None
            result[item.jd_skill_id] = {"tier": item.tier, "sense_key": sense_key, "required": item.required,
                                        "requirement_text": requirement_text}
        if unset:
            raise UnprocessableError(
                "Choose the meaning of each ambiguous skill before calibrating: " + ", ".join(sorted(unset)) + ".")
        return result

    @staticmethod
    def _skill_warnings(core_count: int, ambiguous_without_sense: list) -> list[str]:
        warnings = []
        if core_count > CORE_SKILL_WARNING_THRESHOLD:
            warnings.append(
                f"{core_count} core skills: more than {CORE_SKILL_WARNING_THRESHOLD} makes the core gate strict. "
                f"Consider marking some as supporting.")
        for skill in ambiguous_without_sense:
            warnings.append(f"Choose the meaning of '{skill.name}' (scoring currently assumes "
                            f"'{skill.suggested_sense_key or 'the default'}').")
        return warnings

    @staticmethod
    def _snapshot(jd, rows: dict) -> dict:
        return {
            "skills": {str(sid): {"name": name, "canonical_skill_id": str(row.canonical_skill_id),
                                  "tier": _tier(row.mandatory, row.importance), "sense_key": row.sense_key,
                                  "required": getattr(row, "required", False) is True,
                                  "requirement_text": getattr(row, "requirement_text", None)}
                       for sid, (row, name) in rows.items()},
            "min_experience_years": effective_min_experience(jd),
            "experience_tolerance_years": _float(jd.experience_tolerance_years),
            "education_requirement": jd.education_requirement.value,
            "internship_credit_pct": jd.internship_credit_pct,
            "role_domain": jd_role_domain(jd),
            "profile_template": getattr(jd, "profile_template", None),
        }

    @staticmethod
    def _changes(before: dict, after: dict) -> dict:
        changes: dict = {}
        skill_changes = []
        for sid, new in after["skills"].items():
            if sid not in before["skills"]:
                skill_changes.append({"skill": new["name"], "field": "added", "before": None, "after": new.get("tier")})
                continue
            old = before["skills"][sid]
            # Versions saved before the requirement text existed have none (= None).
            for field in ("tier", "sense_key", "requirement_text"):
                if old.get(field) != new.get(field):
                    skill_changes.append({"skill": new["name"], "field": field,
                                          "before": old.get(field), "after": new.get(field)})
            # Versions saved before the flag existed have no "required" (= not required).
            if bool(old.get("required")) != bool(new.get("required")):
                skill_changes.append({"skill": new["name"], "field": "required",
                                      "before": bool(old.get("required")), "after": bool(new.get("required"))})
        for sid, old in before["skills"].items():
            if sid not in after["skills"]:
                skill_changes.append({"skill": old["name"], "field": "removed", "before": old.get("tier"), "after": None})
        if skill_changes:
            changes["skills"] = skill_changes
        # .get: versions saved before a setting existed don't have it (= unset).
        for field in ("min_experience_years", "experience_tolerance_years", "education_requirement",
                      "internship_credit_pct", "role_domain", "profile_template"):
            if before.get(field) != after.get(field):
                changes[field] = {"before": before.get(field), "after": after.get(field)}
        return changes
