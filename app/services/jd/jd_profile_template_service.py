"""
JD profile templates (Phase 3 Step 7): HR_ADMIN edits the values of the
three seeded templates (audited, with before / after).

A change that affects scoring - internship credit, tolerance, evidence
sections or whether listed-only skills can be confirmed - marks the scored candidates of live campaigns whose JD uses the
template "score out of date", as a calibration change does. Education is
only the form's default for new choices, so it marks nothing.
"""
import uuid
from datetime import datetime, timezone

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.models.jd.profile_templates import JDProfileTemplate
from app.repositories.jd_profile_template_repository import JDProfileTemplateRepository
from app.schemas.jd_profile_template import JDProfileTemplateUpdate
from app.services.campaign.score_out_of_date import mark_campaigns_out_of_date
from app.services.jd.jd_profile_templates import EVIDENCE_SECTION_OPTIONS, REQUIRED_EVIDENCE_SECTIONS

_SCORING_FIELDS = ("internship_credit_pct", "experience_tolerance_years", "evidence_sections",
                   "listed_only_can_confirm")


def template_entity_id(key: str) -> uuid.UUID:
    """A fixed audit entity id per template (audit_logs.entity_id is a UUID; the key is text)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"airs:jd_profile_template:{key}")


def _snapshot(template: JDProfileTemplate) -> dict:
    tolerance = template.experience_tolerance_years
    return {
        "label": template.label, "description": template.description,
        "internship_credit_pct": template.internship_credit_pct,
        "experience_tolerance_years": float(tolerance) if tolerance is not None else None,
        "education_requirement": template.education_requirement,
        "evidence_sections": list(template.evidence_sections or []),
        "listed_only_can_confirm": template.listed_only_can_confirm is True,
    }


def validated_sections(sections: list[str]) -> list[str]:
    unknown = sorted(set(sections) - set(EVIDENCE_SECTION_OPTIONS))
    if unknown:
        raise UnprocessableError("Unknown resume section(s): " + ", ".join(unknown) + ".")
    missing = sorted(REQUIRED_EVIDENCE_SECTIONS - set(sections))
    if missing:
        raise UnprocessableError("Experience always counts as evidence: " + ", ".join(missing) + " is required.")
    return [s for s in EVIDENCE_SECTION_OPTIONS if s in sections]        # fixed order, no duplicates


class JDProfileTemplateService:
    def __init__(self, repository: JDProfileTemplateRepository, audit_service):
        self.repository = repository
        self.audit_service = audit_service

    def list_all(self) -> list[JDProfileTemplate]:
        return self.repository.list_all()

    def update(self, key: str, request: JDProfileTemplateUpdate, *, actor_id: str,
               actor_role: str | None) -> tuple[JDProfileTemplate, bool, int]:
        """Returns (template, changed, candidates marked out of date)."""
        try:
            template = self.repository.get(key)
            if template is None:
                raise NotFoundError("Profile template not found.")
            before = _snapshot(template)
            after = {
                "label": request.label.strip(), "description": (request.description or "").strip() or None,
                "internship_credit_pct": request.internship_credit_pct,
                "experience_tolerance_years": request.experience_tolerance_years,
                "education_requirement": request.education_requirement,
                "evidence_sections": validated_sections(request.evidence_sections),
                "listed_only_can_confirm": request.listed_only_can_confirm,
            }
            if not after["label"]:
                raise UnprocessableError("A template needs a name.")
            changes = {f: {"before": before[f], "after": after[f]} for f in after if before[f] != after[f]}
            if not changes:
                return template, False, 0

            for field, value in after.items():
                setattr(template, field, value)
            now = datetime.now(timezone.utc)
            template.updated_by = actor_id
            template.updated_at = now

            marked = 0
            live_ids = []
            if any(f in changes for f in _SCORING_FIELDS):
                live_ids = self.repository.live_campaign_ids_using(key)
                if live_ids:
                    self.repository.flush()
                    marked = mark_campaigns_out_of_date(self.repository.db, live_ids, (
                        f"The '{template.label}' profile template changed on {now:%Y-%m-%d}: these scores "
                        f"were computed with its previous settings."
                    ))
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.JD_PROFILE_TEMPLATE_UPDATED,
                entity_type=EntityType.JD_PROFILE_TEMPLATE, entity_id=template_entity_id(key),
                details={"key": key, "changes": changes, "live_campaign_ids": [str(i) for i in live_ids],
                         "candidates_marked_out_of_date": marked},
            )
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
        return template, True, marked
