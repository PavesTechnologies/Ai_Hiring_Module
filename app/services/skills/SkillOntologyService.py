import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from fastapi.responses import StreamingResponse
from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from app.models.skills import SkillOntology
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.skill_ontology.skill_ontology_request import (
    SkillCreateRequest,
    SkillOntologyUpdateRequest,
    SkillStatusUpdateRequest,
)
from app.schemas.skill_ontology.skill_ontology_response import (
    BulkImportFailureResponse,
    BulkImportResponse,
    BulkImportValidationErrorResponse,
    BulkImportValidationResponse,
    ParentSkillResponse,
    SimilarSkillResponse,
    SkillCategoryResponse,
    SkillCreateResponse,
    SkillDeactivationImpactResponse,
    SkillHierarchyNodeResponse,
    SkillOntologyChildResponse,
    SkillOntologyListResponse,
    SkillOntologyPageResponse,
    SkillOntologyResponse,
    SkillOntologySummaryResponse,
)
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.skill_ontology_tasks import generate_skill_embedding
from app.utils.excel.skill_excel_reader import SkillExcelReader
from app.utils.excel_export import ExcelExport

logger = logging.getLogger(__name__)

ALLOWED_IMPORT_EXTENSIONS = {".xlsx"}

# update_skill() fields whose before/after values are audit-logged
# (S03-T01, S04-T01). Other editable fields (parent_skill_id, source,
# is_active) are unaffected/out of scope for this audit requirement.
AUDITED_UPDATE_FIELDS = ("canonical_name", "category", "confidence", "aliases")


class SkillOntologyService:
    """Business logic for the Skill Ontology dashboard/list/detail/update/create/import endpoints."""

    SIMILARITY_THRESHOLD_CONFIG_KEY = "SKILL_SIMILARITY_THRESHOLD"
    DEFAULT_SIMILARITY_THRESHOLD = 90.0
    MAX_SIMILAR_SKILLS = 5

    def __init__(
        self,
        repository: SkillOntologyRepository,
        db: Session,
        skill_repository: SkillRepository,
        config_repository: ConfigRepository,
        audit_service: AuditService,
        celery_task_log_repository: CeleryTaskLogRepository,
    ):
        self.repository = repository
        self.db = db
        self.skill_repository = skill_repository
        self.config_repository = config_repository
        self.audit_service = audit_service
        self.celery_task_log_repository = celery_task_log_repository

    def get_dashboard_summary(self) -> SkillOntologySummaryResponse:
        return SkillOntologySummaryResponse(**self.repository.get_dashboard_summary())

    def get_categories(self) -> list[SkillCategoryResponse]:
        return [
            SkillCategoryResponse(category=category, count=count)
            for category, count in self.repository.get_categories()
        ]

    def get_skills(
        self,
        *,
        page: int,
        page_size: int,
        search: str | None,
        category: str | None,
        confidence: str | None,
        is_active: bool | None,
    ) -> SkillOntologyPageResponse:
        rows = self.repository.get_skills(
            page=page,
            page_size=page_size,
            search=search,
            category=category,
            confidence=confidence,
            is_active=is_active,
        )
        total = self.repository.count_skills(
            search=search, category=category, confidence=confidence, is_active=is_active
        )

        items = [
            SkillOntologyListResponse(
                id=skill.id,
                canonical_name=skill.canonical_name,
                aliases=skill.aliases or [],
                category=skill.category,
                parent_skill_name=parent_name,
                confidence=skill.confidence,
                source=skill.source,
                occurrence_count=skill.occurrence_count,
                is_active=skill.is_active,
                created_at=skill.created_at,
            )
            for skill, parent_name in rows
        ]

        return SkillOntologyPageResponse(items=items, page=page, page_size=page_size, total=total)

    def get_skill_detail(self, skill_id: UUID) -> SkillOntologyResponse:
        skill = self._get_skill_or_404(skill_id)

        parent_name = (
            self.repository.get_parent_name(skill.parent_skill_id) if skill.parent_skill_id else None
        )
        children = self.repository.get_children(skill.id)

        return self._to_detail_response(skill, parent_name, children)

    def update_skill(
        self,
        skill_id: UUID,
        request: SkillOntologyUpdateRequest,
        *,
        updated_by: str,
        actor_role: str | None,
    ) -> SkillOntologyResponse:
        """
        Edits canonical_name/category/confidence (S03-T01) plus the
        pre-existing parent_skill_id/source/is_active fields. aliases
        (S04-T01) are merged additively — never overwritten — via
        _merge_aliases(), which also enforces alias uniqueness across the
        whole ontology (S04-T03). remove_aliases (S04-T02) removes individual
        aliases, warning (422, unless confirm_alias_removal=true) when a
        historical candidate_skills match exists for that alias — that table
        is never modified either way. canonical_name changes go through the same
        duplicate-check-excluding-self as before; a real change to
        name/category/confidence/aliases is audit-logged (before/after,
        changed fields only). embedding/embedding_updated_at are never
        touched here — only a canonical_name change re-queues the existing
        embedding task (S03-T03), and the skill stays searchable on its
        current embedding until that task completes.
        """
        canonical_name_changed = False
        try:
            skill = self._get_skill_or_404(skill_id)
            update_data = request.model_dump(exclude_unset=True)

            before: dict[str, str | list[str] | None] = {}
            after: dict[str, str | list[str] | None] = {}

            if "canonical_name" in update_data:
                old_value = skill.canonical_name
                self._apply_canonical_name(skill, update_data["canonical_name"])
                if skill.canonical_name != old_value:
                    before["canonical_name"] = old_value
                    after["canonical_name"] = skill.canonical_name
                    canonical_name_changed = True

            if "aliases" in update_data or "remove_aliases" in update_data:
                old_aliases = list(skill.aliases or [])
                current_aliases = list(old_aliases)

                if "aliases" in update_data:
                    current_aliases = self._merge_aliases(skill, current_aliases, update_data["aliases"])

                if "remove_aliases" in update_data:
                    current_aliases = self._remove_aliases(
                        skill,
                        current_aliases,
                        update_data["remove_aliases"],
                        confirmed=request.confirm_alias_removal,
                    )

                if current_aliases != old_aliases:
                    before["aliases"] = old_aliases
                    after["aliases"] = current_aliases
                skill.aliases = current_aliases

            if "category" in update_data:
                category = update_data["category"]
                old_value = skill.category
                new_value = category.strip() if category else None
                if new_value != old_value:
                    before["category"] = old_value
                    after["category"] = new_value
                skill.category = new_value

            parent_change: dict | None = None
            if "parent_skill_id" in update_data:
                old_parent_id = skill.parent_skill_id
                new_parent_id = self._resolve_parent_skill_id(skill, update_data["parent_skill_id"])
                if new_parent_id != old_parent_id:
                    parent_change = {
                        "before": self._describe_parent(old_parent_id),
                        "after": self._describe_parent(new_parent_id),
                    }
                skill.parent_skill_id = new_parent_id

            if "confidence" in update_data:
                old_value = skill.confidence
                new_value = update_data["confidence"]
                if new_value != old_value:
                    before["confidence"] = old_value
                    after["confidence"] = new_value
                skill.confidence = new_value

            if "source" in update_data:
                skill.source = update_data["source"]

            if "is_active" in update_data:
                skill.is_active = update_data["is_active"]

            if before:
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role=actor_role,
                    action_type=ActionType.SKILL_UPDATED,
                    entity_type=EntityType.SKILL,
                    entity_id=skill.id,
                    details={"before": before, "after": after},
                )
                logger.info("Audit completed | skill_id=%s fields_changed=%s", skill.id, list(before.keys()))

            if parent_change:
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role=actor_role,
                    action_type=ActionType.SKILL_PARENT_UPDATED,
                    entity_type=EntityType.SKILL,
                    entity_id=skill.id,
                    details=parent_change,
                )
                logger.info(
                    "Parent skill updated | skill_id=%s before=%s after=%s",
                    skill.id, parent_change["before"], parent_change["after"],
                )

            self.repository.update_skill(skill)
            self.repository.commit()
            logger.info("Skill updated | skill_id=%s fields_changed=%s", skill.id, list(before.keys()))
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to update skill '%s'.", skill_id)
            raise

        # The update (and its audit log) already committed above. Embedding
        # regeneration is a non-critical side effect that must never block
        # or fail this PATCH response.
        if canonical_name_changed:
            logger.info(
                "Canonical name changed. Queuing embedding regeneration for skill %s",
                skill.id,
            )
            self._enqueue_embedding_generation(skill.id)
        else:
            logger.info(
                "Canonical name unchanged. Skipping embedding regeneration for skill %s",
                skill.id,
            )

        return self.get_skill_detail(skill.id)

    def create_skill(self, request: SkillCreateRequest) -> SkillCreateResponse:
        try:
            canonical_name = request.canonical_name.strip()
            if not canonical_name:
                raise UnprocessableError("canonical_name cannot be empty.")

            if self.repository.get_by_canonical_name_exact(canonical_name):
                raise ConflictError(f"Skill '{canonical_name}' already exists.")

            parent_skill_id = request.parent_skill_id
            if parent_skill_id is not None and not self.repository.get_skill_by_id(parent_skill_id):
                raise UnprocessableError(f"Parent skill '{parent_skill_id}' does not exist.")

            aliases = self._clean_aliases(request.aliases)

            skill = SkillOntology(
                id=uuid4(),
                canonical_name=canonical_name,
                aliases=aliases,
                category=request.category.strip() if request.category else None,
                parent_skill_id=parent_skill_id,
                confidence=request.confidence,
                source=request.source,
                is_active=request.is_active,
            )
            self.repository.create_skill(skill)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to create skill '%s'.", request.canonical_name)
            raise

        # The skill is already committed at this point. Embedding generation
        # and similar-skill suggestions are non-critical side effects that
        # must never turn a successful creation into a failed response.
        self._enqueue_embedding_generation(skill.id)

        response = SkillCreateResponse.model_validate(skill)
        response.similar_skills = self._find_similar_skills(canonical_name, exclude_id=skill.id)
        return response

    def get_deactivation_impact(self, skill_id: UUID) -> SkillDeactivationImpactResponse:
        """
        S06-T01: read-only preview called before the confirm dialog — never
        changes is_active. Surfaces candidate_skills/jd_skills usage counts
        and, if any exist, the immediate children that will need
        child_handling='PROMOTE'|'ROOT' when the actual deactivation is
        submitted.
        """
        skill = self._get_skill_or_404(skill_id)

        candidate_usage = self.repository.count_candidate_usage(skill.id)
        jd_usage = self.repository.count_jd_usage(skill.id)
        children = self.repository.get_children(skill.id)
        child_names = [child.canonical_name for child in children]

        logger.info(
            "Deactivation impact checked | skill_id=%s candidate_usage=%s jd_usage=%s children=%s",
            skill.id, candidate_usage, jd_usage, len(child_names),
        )

        if not skill.is_active:
            return SkillDeactivationImpactResponse(
                can_deactivate=False,
                candidate_usage=candidate_usage,
                jd_usage=jd_usage,
                warning=f"Skill '{skill.canonical_name}' is already inactive.",
                children=child_names,
            )

        return SkillDeactivationImpactResponse(
            can_deactivate=True,
            candidate_usage=candidate_usage,
            jd_usage=jd_usage,
            warning=self._build_deactivation_warning(candidate_usage, jd_usage, len(child_names)),
            children=child_names,
        )

    def update_status(
        self,
        skill_id: UUID,
        request: SkillStatusUpdateRequest,
        *,
        updated_by: str,
        actor_role: str | None,
    ) -> SkillOntologyResponse:
        """
        S05-T03/S06-T02/T03: toggles is_active. UUID/aliases/embedding/parent
        are never touched here (soft delete only). Deactivating a skill with
        children requires child_handling ('PROMOTE': children move up to
        this skill's own parent; 'ROOT': children become root skills;
        'CANCEL': abort — nothing changes, not even is_active) — applied via
        a single bulk UPDATE, atomically with the status change and its
        audit log. Reactivating never touches children/hierarchy.
        """
        if request.child_handling == "CANCEL":
            logger.info("Skill status change cancelled by user | skill_id=%s", skill_id)
            return self.get_skill_detail(skill_id)

        try:
            skill = self._get_skill_or_404(skill_id)
            old_status = skill.is_active
            new_status = request.is_active

            if old_status == new_status:
                state = "active" if new_status else "inactive"
                raise ConflictError(f"Skill '{skill.canonical_name}' is already {state}.")

            affected_child_count = 0
            child_handling = request.child_handling

            if not new_status:
                children = self.repository.get_children(skill.id)
                if children:
                    if child_handling is None:
                        child_names = [child.canonical_name for child in children]
                        raise UnprocessableError(
                            f"Skill '{skill.canonical_name}' has {len(children)} child skill(s) "
                            f"({', '.join(child_names)}). Specify child_handling='PROMOTE' "
                            f"(move them under this skill's own parent) or 'ROOT' (make them root "
                            f"skills) to proceed."
                        )

                    new_parent_for_children = skill.parent_skill_id if child_handling == "PROMOTE" else None
                    affected_child_count = self.repository.reparent_children(skill.id, new_parent_for_children)
                    logger.info(
                        "Children reassigned | skill_id=%s child_handling=%s affected_child_count=%s",
                        skill.id, child_handling, affected_child_count,
                    )

            skill.is_active = new_status
            self.repository.update_skill(skill)

            action_type = ActionType.SKILL_REACTIVATED if new_status else ActionType.SKILL_DEACTIVATED
            self.audit_service.log(
                actor_id=updated_by,
                actor_role=actor_role,
                action_type=action_type,
                entity_type=EntityType.SKILL,
                entity_id=skill.id,
                details={
                    "old_status": "active" if old_status else "inactive",
                    "new_status": "active" if new_status else "inactive",
                    "child_handling": child_handling,
                    "affected_child_count": affected_child_count,
                },
            )

            self.repository.commit()
            logger.info(
                "Skill status updated | skill_id=%s old_status=%s new_status=%s",
                skill.id, old_status, new_status,
            )
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to update status for skill '%s'.", skill_id)
            raise

        return self.get_skill_detail(skill.id)

    @staticmethod
    def _build_deactivation_warning(candidate_usage: int, jd_usage: int, child_count: int) -> str | None:
        parts: list[str] = []
        if candidate_usage > 0 and jd_usage > 0:
            parts.append("This skill is currently referenced by candidates and job descriptions.")
        elif candidate_usage > 0:
            parts.append("This skill is currently referenced by candidates.")
        elif jd_usage > 0:
            parts.append("This skill is currently referenced by job descriptions.")

        if child_count > 0:
            parts.append(
                f"This skill has {child_count} child skill(s) that must be promoted or made root skills."
            )

        return " ".join(parts) if parts else None

    def get_parents(
        self, search: str | None, exclude_skill_id: UUID | None = None
    ) -> list[ParentSkillResponse]:
        """
        S05-T01: when exclude_skill_id is given (editing an existing skill),
        excludes that skill itself and every one of its descendants from the
        candidate list — assigning a descendant as parent would create a
        circular hierarchy, so it's kept out of the picker entirely rather
        than merely rejected after the fact.
        """
        exclude_ids: set[UUID] | None = None
        if exclude_skill_id is not None:
            exclude_ids = self.repository.get_descendant_ids(exclude_skill_id)
            exclude_ids.add(exclude_skill_id)

        skills = self.repository.get_parents(search=search, exclude_ids=exclude_ids, limit=20)
        return [
            ParentSkillResponse(id=skill.id, canonical_name=skill.canonical_name) for skill in skills
        ]

    def get_hierarchy_roots(self) -> list[SkillHierarchyNodeResponse]:
        """S05-T02: top level of the hierarchy tree — skills with no parent."""
        rows = self.repository.get_root_skills()
        return [self._to_hierarchy_node(skill, has_children) for skill, has_children in rows]

    def get_hierarchy_children(self, skill_id: UUID) -> list[SkillHierarchyNodeResponse]:
        """S05-T02: immediate children only — the tree lazy-loads one level per expand."""
        self._get_skill_or_404(skill_id)
        rows = self.repository.get_children_with_has_children(skill_id)
        return [self._to_hierarchy_node(skill, has_children) for skill, has_children in rows]

    @staticmethod
    def _to_hierarchy_node(skill: SkillOntology, has_children: bool) -> SkillHierarchyNodeResponse:
        return SkillHierarchyNodeResponse(
            id=skill.id,
            canonical_name=skill.canonical_name,
            confidence=skill.confidence,
            is_active=skill.is_active,
            has_children=bool(has_children),
        )

    def export_skills(
        self,
        *,
        search: str | None,
        category: str | None,
        confidence: str | None,
        is_active: bool | None,
    ) -> StreamingResponse:
        rows = self.repository.get_skills_for_export(
            search=search, category=category, confidence=confidence, is_active=is_active
        )
        excel_file = ExcelExport.export_skill_ontology(rows)
        filename = f"skill_ontology_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def validate_bulk_import(self, file: UploadFile) -> BulkImportValidationResponse:
        """
        S07-T01: dry-run validation — reuses SkillExcelReader for parsing but
        never writes to the database (see _execute_bulk_import for the
        actual POST /import path). success is False only when the file
        itself couldn't be parsed at all (missing required columns, unread-
        able file); per-row issues are always reported via validation_errors
        without blocking the response, so the caller can see the full
        picture before deciding whether to proceed with the real import.
        """
        if not file.filename or Path(file.filename).suffix.lower() not in ALLOWED_IMPORT_EXTENSIONS:
            raise BadRequestError("Only .xlsx files are supported for bulk import.")

        tmp_path = self._save_upload_to_temp(file)
        try:
            try:
                skills = SkillExcelReader.read(tmp_path)
            except (ValueError, FileNotFoundError) as exc:
                logger.warning("Bulk import validation failed at file level | reason=%s", exc)
                return BulkImportValidationResponse(
                    success=False,
                    validation_errors=[BulkImportValidationErrorResponse(message=str(exc))],
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        return self._validate_rows(skills)

    def bulk_import(self, file: UploadFile, *, updated_by: str, actor_role: str | None) -> BulkImportResponse:
        """
        S07-T02: parses via the same SkillExcelReader as validate_bulk_import
        and the original seed flow, then inserts new canonical_names /
        updates existing ones (audit-logged like a normal PATCH) row by row.
        Each row runs in its own SAVEPOINT (db.begin_nested()) so a single
        bad row is skipped/failed without discarding any other row already
        processed in this same request — a plain try/except around a shared
        transaction is not enough for that guarantee once a row reaches the
        database (a failed statement poisons the whole transaction in
        Postgres unless it was inside its own savepoint).
        """
        if not file.filename or Path(file.filename).suffix.lower() not in ALLOWED_IMPORT_EXTENSIONS:
            raise BadRequestError("Only .xlsx files are supported for bulk import.")

        tmp_path = self._save_upload_to_temp(file)
        try:
            try:
                skills = SkillExcelReader.read(tmp_path)
            except (ValueError, FileNotFoundError) as exc:
                raise BadRequestError(str(exc)) from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        return self._execute_bulk_import(
            skills, file_name=file.filename, updated_by=updated_by, actor_role=actor_role
        )

    @staticmethod
    def _save_upload_to_temp(file: UploadFile) -> str:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(file.file.read())
            return tmp.name

    def _validate_rows(self, skills: list[dict]) -> BulkImportValidationResponse:
        existing_by_name = self.repository.get_all_canonical_names()
        seen_in_file: set[str] = set()
        errors: list[BulkImportValidationErrorResponse] = []

        for skill in skills:
            row_number = skill["row_number"]
            canonical_name = skill["canonical_name"]

            if not canonical_name:
                errors.append(BulkImportValidationErrorResponse(
                    row=row_number, column="canonical_name", message="canonical_name is required.",
                ))
                continue  # remaining checks are meaningless without a name

            if skill["confidence"] not in ("verified", "unverified"):
                errors.append(BulkImportValidationErrorResponse(
                    row=row_number, column="confidence",
                    message="Confidence must be 'verified' or 'unverified'.",
                ))

            if canonical_name in seen_in_file:
                errors.append(BulkImportValidationErrorResponse(
                    row=row_number, column="canonical_name",
                    message="Duplicate canonical_name within uploaded file.",
                ))
            else:
                seen_in_file.add(canonical_name)
                if canonical_name in existing_by_name:
                    errors.append(BulkImportValidationErrorResponse(
                        row=row_number, column="canonical_name", message="Skill already exists.",
                    ))

            parent_name = skill["parent_skill"]
            if parent_name and parent_name not in existing_by_name and parent_name not in seen_in_file:
                errors.append(BulkImportValidationErrorResponse(
                    row=row_number, column="parent_skill",
                    message=f"Parent skill '{parent_name}' does not exist.",
                ))

            if any(not alias for alias in skill["aliases"]):
                errors.append(BulkImportValidationErrorResponse(
                    row=row_number, column="aliases", message="Aliases must not contain blank values.",
                ))

        total_rows = len(skills)
        invalid_rows = len({error.row for error in errors if error.row is not None})
        valid_rows = total_rows - invalid_rows

        logger.info(
            "Bulk import validation completed | total_rows=%s valid_rows=%s invalid_rows=%s",
            total_rows, valid_rows, invalid_rows,
        )

        return BulkImportValidationResponse(
            success=True,
            total_rows=total_rows,
            valid_rows=valid_rows,
            invalid_rows=invalid_rows,
            validation_errors=errors,
        )

    def _execute_bulk_import(
        self, skills: list[dict], *, file_name: str, updated_by: str, actor_role: str | None
    ) -> BulkImportResponse:
        inserted = updated = skipped = failed = 0
        failures: list[BulkImportFailureResponse] = []
        failed_rows_detail: list[dict] = []

        try:
            existing_by_name = self.repository.get_all_canonical_names()
            seen_in_file: set[str] = set()

            for skill in skills:
                row_number = skill["row_number"]
                canonical_name = skill["canonical_name"]

                try:
                    if not canonical_name:
                        raise ValueError("canonical_name is required.")

                    if skill["confidence"] not in ("verified", "unverified"):
                        raise ValueError("Confidence must be 'verified' or 'unverified'.")

                    if any(not alias for alias in skill["aliases"]):
                        raise ValueError("Aliases must not contain blank values.")

                    if canonical_name in seen_in_file:
                        skipped += 1
                        logger.info(
                            "Bulk import row skipped | row=%s reason=duplicate_in_file canonical_name=%s",
                            row_number, canonical_name,
                        )
                        continue
                    seen_in_file.add(canonical_name)

                    parent_skill_id = None
                    if skill["parent_skill"]:
                        parent = existing_by_name.get(skill["parent_skill"])
                        if not parent:
                            raise ValueError(f"Parent skill '{skill['parent_skill']}' does not exist.")
                        parent_skill_id = parent.id

                    cleaned_aliases = self._clean_aliases(skill["aliases"])

                    # Each row gets its own SAVEPOINT: if this row's insert/
                    # update fails at the database level, only this row's
                    # work is rolled back — every previously-processed row
                    # in this same request stays committed-pending.
                    with self.db.begin_nested():
                        existing_skill = existing_by_name.get(canonical_name)
                        if existing_skill:
                            self._apply_bulk_update(
                                existing_skill, skill, cleaned_aliases, parent_skill_id,
                                updated_by=updated_by, actor_role=actor_role,
                            )
                            updated += 1
                        else:
                            new_skill = SkillOntology(
                                id=uuid4(),
                                canonical_name=canonical_name,
                                aliases=cleaned_aliases,
                                category=skill["category"],
                                parent_skill_id=parent_skill_id,
                                confidence=skill["confidence"],
                                source=skill["source"],
                                is_active=skill["is_active"],
                            )
                            self.repository.create_skill(new_skill)
                            existing_by_name[canonical_name] = new_skill
                            inserted += 1

                except Exception as exc:
                    failed += 1
                    reason = str(exc)
                    failures.append(BulkImportFailureResponse(row=row_number, reason=reason))
                    failed_rows_detail.append({
                        "row": row_number,
                        "canonical_name": canonical_name,
                        "aliases": skill.get("aliases", []),
                        "category": skill.get("category"),
                        "parent_skill": skill.get("parent_skill"),
                        "confidence": skill.get("confidence"),
                        "reason": reason,
                    })
                    logger.exception("Bulk import row failed | row=%s reason=%s", row_number, reason)

            self.repository.commit()
        except Exception:
            self.repository.rollback()
            logger.exception("Bulk import failed.")
            raise

        logger.info(
            "Bulk import completed | inserted=%s updated=%s skipped=%s failed=%s",
            inserted, updated, skipped, failed,
        )

        import_id = None
        if failed_rows_detail:
            import_id = uuid4()
            self._store_import_error_report(import_id, file_name, failed_rows_detail)

        return BulkImportResponse(
            inserted=inserted, updated=updated, skipped=skipped, failed=failed,
            failures=failures, import_id=import_id,
        )

    def _store_import_error_report(self, import_id: UUID, file_name: str, failed_rows_detail: list[dict]) -> None:
        """
        S07-T03: stores metadata (import_id, file_name, created_at,
        failed_row_count) plus the full failed-row detail needed to
        regenerate the Excel report later — reusing the existing
        CeleryTaskLogRepository/Service as-is (task_id/task_type/
        output_summary are plain unconstrained columns, so this needs no
        new table or schema change). Not the Celery broker itself — this
        import runs synchronously; the log row is just being reused as a
        keyed, timestamped store for the report contents.
        """
        task_log_service = CeleryTaskLogService(self.celery_task_log_repository)
        log = task_log_service.create_log(task_id=str(import_id), task_type="SKILL_BULK_IMPORT_ERROR_REPORT")
        summary = json.dumps({
            "file_name": file_name,
            "failed_row_count": len(failed_rows_detail),
            "failures": failed_rows_detail,
        })
        task_log_service.mark_success(log, summary=summary)
        logger.info(
            "Import error report stored | import_id=%s file_name=%s failed_row_count=%s",
            import_id, file_name, len(failed_rows_detail),
        )

    def get_import_error_report(self, import_id: UUID) -> StreamingResponse:
        """S07-T03: regenerates the failed-rows Excel report from the stored CeleryTaskLog entry."""
        task_log = self.celery_task_log_repository.get_by_task_id(str(import_id))
        if not task_log or task_log.task_type != "SKILL_BULK_IMPORT_ERROR_REPORT":
            raise NotFoundError(f"Import report '{import_id}' not found.")

        summary = json.loads(task_log.output_summary or "{}")
        failures = summary.get("failures", [])

        excel_file = ExcelExport.export_bulk_import_errors(failures)
        filename = f"import_errors_{import_id}.xlsx"

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _apply_bulk_update(
        self,
        skill: SkillOntology,
        skill_row: dict,
        cleaned_aliases: list[str],
        parent_skill_id: UUID | None,
        *,
        updated_by: str,
        actor_role: str | None,
    ) -> None:
        """Bulk import's row already exists as a canonical_name — updates it exactly like a normal PATCH would, audit-logged the same way."""
        before: dict = {}
        after: dict = {}

        for field, new_value in (
            ("category", skill_row["category"]),
            ("confidence", skill_row["confidence"]),
            ("source", skill_row["source"]),
            ("is_active", skill_row["is_active"]),
        ):
            old_value = getattr(skill, field)
            if new_value != old_value:
                before[field] = old_value
                after[field] = new_value
            setattr(skill, field, new_value)

        if cleaned_aliases != (skill.aliases or []):
            before["aliases"] = skill.aliases or []
            after["aliases"] = cleaned_aliases
        skill.aliases = cleaned_aliases

        if parent_skill_id != skill.parent_skill_id:
            before["parent_skill_id"] = str(skill.parent_skill_id) if skill.parent_skill_id else None
            after["parent_skill_id"] = str(parent_skill_id) if parent_skill_id else None
        skill.parent_skill_id = parent_skill_id

        self.repository.update_skill(skill)

        if before:
            self.audit_service.log(
                actor_id=updated_by,
                actor_role=actor_role,
                action_type=ActionType.SKILL_UPDATED,
                entity_type=EntityType.SKILL,
                entity_id=skill.id,
                details={"before": before, "after": after, "source": "bulk_import"},
            )

    def _apply_canonical_name(self, skill: SkillOntology, canonical_name: str) -> None:
        trimmed = canonical_name.strip()
        if not trimmed:
            raise UnprocessableError("canonical_name cannot be empty.")

        if trimmed != skill.canonical_name:
            duplicate = self.repository.get_by_canonical_name_exact(trimmed)
            if duplicate and duplicate.id != skill.id:
                raise ConflictError(f"Skill '{trimmed}' already exists.")

        skill.canonical_name = trimmed

    def _resolve_parent_skill_id(self, skill: SkillOntology, parent_skill_id: UUID | None) -> UUID | None:
        if parent_skill_id is None:
            return None

        if parent_skill_id == skill.id:
            raise UnprocessableError("A skill cannot be its own parent.")

        parent = self.repository.get_skill_by_id(parent_skill_id)
        if not parent:
            raise UnprocessableError(f"Parent skill '{parent_skill_id}' does not exist.")

        if not parent.is_active:
            raise UnprocessableError(f"Parent skill '{parent.canonical_name}' is not active.")

        if self._creates_circular_hierarchy(skill.id, parent_skill_id):
            logger.info(
                "Circular hierarchy blocked | skill_id=%s proposed_parent_id=%s", skill.id, parent_skill_id
            )
            raise ConflictError("Circular hierarchy detected.")

        return parent_skill_id

    def _creates_circular_hierarchy(self, skill_id: UUID, proposed_parent_id: UUID) -> bool:
        """
        S05-T01: walks proposed_parent_id's ancestor chain upward; True if
        skill_id appears anywhere in it (i.e. proposed_parent_id is one of
        skill_id's own descendants, which would make the tree circular).
        """
        current_id: UUID | None = proposed_parent_id
        visited: set[UUID] = set()
        while current_id is not None:
            if current_id == skill_id:
                return True
            if current_id in visited:
                break  # defensive guard against a pre-existing cycle in the data
            visited.add(current_id)
            parent = self.repository.get_skill_by_id(current_id)
            current_id = parent.parent_skill_id if parent else None
        return False

    def _describe_parent(self, parent_skill_id: UUID | None) -> dict[str, str | None]:
        if parent_skill_id is None:
            return {"id": None, "canonical_name": None}
        return {"id": str(parent_skill_id), "canonical_name": self.repository.get_parent_name(parent_skill_id)}

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

    def _merge_aliases(
        self, skill: SkillOntology, current_aliases: list[str], incoming_aliases: list[str] | None
    ) -> list[str]:
        """
        S04-T01: additive merge — the request's aliases are added to
        current_aliases, never replacing them.
        S04-T03: each new alias is validated against the whole ontology
        before being added — silently skipped if it already belongs to this
        same skill, rejected with 409 if it belongs to any other skill.
        """
        existing_set = set(current_aliases)
        cleaned_incoming = self._clean_aliases(incoming_aliases)

        logger.info(
            "Alias validation started | skill_id=%s existing=%s incoming=%s",
            skill.id, current_aliases, cleaned_incoming,
        )

        merged = list(current_aliases)
        for alias in cleaned_incoming:
            if alias in existing_set:
                continue  # already belongs to this skill — not an error, just a no-op

            conflicting_skill = self.repository.find_skill_by_alias(alias, exclude_id=skill.id)
            if conflicting_skill:
                logger.info(
                    "Alias conflict detected | alias='%s' conflicts_with_skill='%s' (id=%s)",
                    alias, conflicting_skill.canonical_name, conflicting_skill.id,
                )
                raise ConflictError(
                    f"Alias '{alias}' already belongs to canonical skill '{conflicting_skill.canonical_name}'."
                )

            merged.append(alias)
            existing_set.add(alias)
            logger.info("Alias added | skill_id=%s alias='%s'", skill.id, alias)

        return merged

    def _remove_aliases(
        self,
        skill: SkillOntology,
        current_aliases: list[str],
        aliases_to_remove: list[str] | None,
        *,
        confirmed: bool,
    ) -> list[str]:
        """
        S04-T02: removes the requested aliases from current_aliases (Python-
        side list rebuild — the SQLAlchemy equivalent of ARRAY_REMOVE, since
        this same method already rebuilds the array in Python for T01).

        Before removing, checks candidate_skills for historical rows that
        matched via this alias (match_tier='alias'). candidate_skills is
        NEVER modified — historical mappings must remain unchanged — but if
        any alias has affected rows and the caller hasn't set
        confirm_alias_removal=true, the removal is rejected with a 422
        naming the affected counts so the admin can confirm knowingly.
        """
        targets = [alias for alias in self._clean_aliases(aliases_to_remove) if alias in current_aliases]
        if not targets:
            return current_aliases

        warnings = [
            (alias, affected)
            for alias in targets
            for affected in [self.repository.count_candidate_matches_by_alias(alias)]
            if affected > 0
        ]

        if warnings and not confirmed:
            logger.info(
                "Alias removal requires confirmation | skill_id=%s warnings=%s", skill.id, warnings
            )
            details = "; ".join(
                f"'{alias}' is referenced by {count} historical candidate match(es)"
                for alias, count in warnings
            )
            raise UnprocessableError(
                f"{details}. These historical candidate_skills mappings will NOT be changed if you "
                f"proceed. Re-submit with confirm_alias_removal=true to remove the alias(es) anyway."
            )

        target_set = set(targets)
        remaining = [alias for alias in current_aliases if alias not in target_set]
        for alias in targets:
            logger.info("Alias removed | skill_id=%s alias='%s'", skill.id, alias)

        return remaining

    @staticmethod
    def _to_detail_response(
        skill: SkillOntology, parent_name: str | None, children: list[SkillOntology]
    ) -> SkillOntologyResponse:
        return SkillOntologyResponse(
            id=skill.id,
            canonical_name=skill.canonical_name,
            aliases=skill.aliases or [],
            category=skill.category,
            parent_skill_name=parent_name,
            confidence=skill.confidence,
            source=skill.source,
            occurrence_count=skill.occurrence_count,
            is_active=skill.is_active,
            created_at=skill.created_at,
            children=[
                SkillOntologyChildResponse(id=child.id, canonical_name=child.canonical_name)
                for child in children
            ],
            embedding_status="Generated" if skill.embedding is not None else "Pending",
        )

    def _enqueue_embedding_generation(self, skill_id: UUID) -> None:
        try:
            task_id = uuid4()
            generate_skill_embedding.apply_async(
                kwargs={"task_id": str(task_id), "skill_id": str(skill_id)},
                task_id=str(task_id),
            )
        except Exception:
            # Fire-and-forget: a broker outage must never fail skill creation,
            # which has already committed by the time this runs.
            logger.exception("Failed to enqueue embedding generation for skill '%s'.", skill_id)

    def _get_similarity_threshold(self) -> float:
        configs = self.config_repository.get_configs_by_keys([self.SIMILARITY_THRESHOLD_CONFIG_KEY])
        return float(configs.get(self.SIMILARITY_THRESHOLD_CONFIG_KEY, self.DEFAULT_SIMILARITY_THRESHOLD))

    def _find_similar_skills(self, canonical_name: str, *, exclude_id: UUID) -> list[SimilarSkillResponse]:
        """
        Compares the new skill's canonical_name against every existing
        skill's canonical_name + aliases (RapidFuzz plain ratio, matching
        SkillNormalizationService's convention). Read-only: never merges,
        never creates aliases — recommendations only.
        """
        threshold = self._get_similarity_threshold()
        logger.info(
            "Similarity check started | new_skill='%s' threshold=%.2f", canonical_name, threshold
        )

        catalog = [
            skill for skill in self.skill_repository.list_active_skills() if skill.id != exclude_id
        ]
        if not catalog:
            return []

        choices: dict[str, SkillOntology] = {}
        for skill in catalog:
            choices.setdefault(skill.canonical_name, skill)
            for alias in skill.aliases or []:
                choices.setdefault(alias, skill)

        # Plain ratio (not WRatio), matching SkillNormalizationService:
        # WRatio's partial-ratio component would false-positive on substring
        # pairs like "Java" vs "JavaScript".
        matches = process.extract(
            canonical_name, choices.keys(), scorer=fuzz.ratio, limit=None, score_cutoff=threshold
        )
        logger.info("RapidFuzz raw matches: %s", matches)
        best_scores: dict[UUID, float] = {}
        for matched_text, score, _ in matches:
            matched_skill = choices[matched_text]
            if score > best_scores.get(matched_skill.id, 0):
                best_scores[matched_skill.id] = score

        skills_by_id = {skill.id: skill for skill in catalog}
        results = [
            SimilarSkillResponse(
                id=skill_id,
                canonical_name=skills_by_id[skill_id].canonical_name,
                category=skills_by_id[skill_id].category,
                similarity_score=round(score),
            )
            for skill_id, score in best_scores.items()
        ]
        results.sort(key=lambda item: item.similarity_score, reverse=True)
        results = results[: self.MAX_SIMILAR_SKILLS]

        for match in results:
            logger.info(
                "Similar skill matched | new_skill='%s' matched_skill='%s' (id=%s) score=%d threshold=%.2f",
                canonical_name,
                match.canonical_name,
                match.id,
                match.similarity_score,
                threshold,
            )

        return results

    def _get_skill_or_404(self, skill_id: UUID) -> SkillOntology:
        skill = self.repository.get_skill_by_id(skill_id)
        if not skill:
            raise NotFoundError(f"Skill '{skill_id}' not found.")
        return skill
