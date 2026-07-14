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
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.skill_ontology.skill_ontology_request import (
    SkillCreateRequest,
    SkillOntologyUpdateRequest,
    SkillStatusUpdateRequest,
)
from app.schemas.skill_ontology.skill_ontology_response import (
    BulkImportResponse,
    ParentSkillResponse,
    SimilarSkillResponse,
    SkillCategoryResponse,
    SkillCreateResponse,
    SkillOntologyChildResponse,
    SkillOntologyListResponse,
    SkillOntologyPageResponse,
    SkillOntologyResponse,
    SkillOntologySummaryResponse,
)
from app.services.audit_service import AuditService
from app.services.skill_seed_service import SkillSeedService
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
    ):
        self.repository = repository
        self.db = db
        self.skill_repository = skill_repository
        self.config_repository = config_repository
        self.audit_service = audit_service

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

            if "parent_skill_id" in update_data:
                skill.parent_skill_id = self._resolve_parent_skill_id(skill, update_data["parent_skill_id"])

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

    def update_status(self, skill_id: UUID, request: SkillStatusUpdateRequest) -> SkillOntologyResponse:
        try:
            skill = self._get_skill_or_404(skill_id)
            skill.is_active = request.is_active
            self.repository.update_skill(skill)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to update status for skill '%s'.", skill_id)
            raise

        return self.get_skill_detail(skill.id)

    def get_parents(self, search: str | None) -> list[ParentSkillResponse]:
        skills = self.repository.get_parents(search=search, limit=20)
        return [
            ParentSkillResponse(id=skill.id, canonical_name=skill.canonical_name) for skill in skills
        ]

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

    def bulk_import(self, file: UploadFile) -> BulkImportResponse:
        if not file.filename or Path(file.filename).suffix.lower() not in ALLOWED_IMPORT_EXTENSIONS:
            raise BadRequestError("Only .xlsx files are supported for bulk import.")

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(file.file.read())
            tmp_path = tmp.name

        try:
            try:
                skills = SkillExcelReader.read(tmp_path)
            except (ValueError, FileNotFoundError) as exc:
                raise BadRequestError(str(exc)) from exc

            summary = SkillSeedService(self.db).seed_skills(skills)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        return BulkImportResponse(**summary)

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

        if not self.repository.get_skill_by_id(parent_skill_id):
            raise UnprocessableError(f"Parent skill '{parent_skill_id}' does not exist.")

        return parent_skill_id

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
