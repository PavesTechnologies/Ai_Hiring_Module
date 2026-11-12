"""
Related education fields per role domain (Phase 3 Step 2).

HR_ADMIN adds and removes pairs (audited); the education gate reads the
pairs for a JD's role domain when its education is mandatory
(field_families_for_jd). A pair is unordered and never a field with itself.
"""
from uuid import UUID

from app.services.settings.reference_impact import mark_after_edit
from app.enums.constants import ActionType, EntityType
from app.enums.education import EducationField, RoleDomain
from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.education import EducationFieldRelation
from app.repositories.education_field_relation_repository import EducationFieldRelationRepository
from app.services.jd.jd_calibration_rules import EDUCATION_MANDATORY, jd_education_requirement, jd_role_domain

# Fields a pair can name: the controlled vocabulary minus the "not classified" values.
PAIRABLE_FIELDS = tuple(f.value for f in EducationField if f not in (EducationField.OTHER, EducationField.UNKNOWN))


def ordered_pair(field_1: str, field_2: str) -> tuple[str, str]:
    return (field_1, field_2) if field_1 < field_2 else (field_2, field_1)


def field_families_for_jd(db, job_description) -> frozenset:
    """
    The related-field pairs the education gate applies to this JD: those of
    its role domain, when its education is mandatory and a domain is set;
    otherwise none. Each pair is a frozenset of two field values.
    """
    domain = jd_role_domain(job_description)
    if domain is None or jd_education_requirement(job_description) != EDUCATION_MANDATORY:
        return frozenset()
    return frozenset(frozenset({r.field_a, r.field_b})
                     for r in EducationFieldRelationRepository(db).for_domain(domain))


class EducationFieldFamilyService:
    def __init__(self, repository: EducationFieldRelationRepository, audit_service):
        self.repository = repository
        self.audit_service = audit_service

    def list_all(self) -> list[EducationFieldRelation]:
        return self.repository.list_all()

    def add(self, *, field_1: str, field_2: str, role_domain: str, note: str | None, actor_id: str,
            actor_role: str | None) -> EducationFieldRelation:
        for field in (field_1, field_2):
            if field not in PAIRABLE_FIELDS:
                raise UnprocessableError(f"Unknown field of study: {field}.")
        if role_domain not in {d.value for d in RoleDomain}:
            raise UnprocessableError(f"Unknown role domain: {role_domain}.")
        if field_1 == field_2:
            raise UnprocessableError("A field is always related to itself - choose two different fields.")
        field_a, field_b = ordered_pair(field_1, field_2)
        try:
            if self.repository.find(field_a, field_b, role_domain) is not None:
                raise ConflictError("These two fields are already related for this role domain.")
            relation = self.repository.add(EducationFieldRelation(
                field_a=field_a, field_b=field_b, role_domain=role_domain,
                note=(note or "").strip() or None, created_by=actor_id,
            ))
            # Settings phase 3: scored candidates of live campaigns this pair can change are stamped out of date.
            marked = mark_after_edit(self.repository.db, "education_relation", "A related education field pair",
                                     role_domain=role_domain)
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.EDUCATION_FIELD_RELATION_ADDED,
                entity_type=EntityType.EDUCATION_FIELD_RELATION, entity_id=relation.id,
                details={"field_a": field_a, "field_b": field_b, "role_domain": role_domain, "note_provided": bool(relation.note),
                         "candidates_marked_out_of_date": marked},
            )
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
        return relation

    def remove(self, relation_id: UUID, *, actor_id: str, actor_role: str | None) -> None:
        try:
            relation = self.repository.get(relation_id)
            if relation is None:
                raise NotFoundError("Related-field pair not found.")
            details = {"field_a": relation.field_a, "field_b": relation.field_b,
                       "role_domain": relation.role_domain, "note_provided": bool(relation.note)}
            self.repository.delete(relation)
            details["candidates_marked_out_of_date"] = mark_after_edit(
                self.repository.db, "education_relation", "A related education field pair",
                role_domain=details["role_domain"])
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.EDUCATION_FIELD_RELATION_REMOVED,
                entity_type=EntityType.EDUCATION_FIELD_RELATION, entity_id=relation_id, details=details,
            )
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
