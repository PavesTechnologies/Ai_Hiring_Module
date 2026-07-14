import logging
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.exception_handler.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from app.models.skills import SkillOntology
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.schemas.skill_ontology.skill_ontology_request import (
    SkillCreateRequest,
    SkillOntologyUpdateRequest,
    SkillStatusUpdateRequest,
)
from app.schemas.skill_ontology.skill_ontology_response import (
    BulkImportResponse,
    ParentSkillResponse,
    SkillCategoryResponse,
    SkillCreateResponse,
    SkillOntologyChildResponse,
    SkillOntologyListResponse,
    SkillOntologyPageResponse,
    SkillOntologyResponse,
    SkillOntologySummaryResponse,
)
from app.services.skill_seed_service import SkillSeedService
from app.utils.excel.skill_excel_reader import SkillExcelReader
from app.utils.excel_export import ExcelExport

logger = logging.getLogger(__name__)

ALLOWED_IMPORT_EXTENSIONS = {".xlsx"}


class SkillOntologyService:
    """Business logic for the Skill Ontology dashboard/list/detail/update/create/import endpoints."""

    def __init__(self, repository: SkillOntologyRepository, db: Session):
        self.repository = repository
        self.db = db

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

    def update_skill(self, skill_id: UUID, request: SkillOntologyUpdateRequest) -> SkillOntologyResponse:
        try:
            skill = self._get_skill_or_404(skill_id)
            update_data = request.model_dump(exclude_unset=True)

            if "canonical_name" in update_data:
                self._apply_canonical_name(skill, update_data["canonical_name"])

            if "aliases" in update_data:
                skill.aliases = self._clean_aliases(update_data["aliases"])

            if "category" in update_data:
                category = update_data["category"]
                skill.category = category.strip() if category else None

            if "parent_skill_id" in update_data:
                skill.parent_skill_id = self._resolve_parent_skill_id(skill, update_data["parent_skill_id"])

            if "confidence" in update_data:
                skill.confidence = update_data["confidence"]

            if "source" in update_data:
                skill.source = update_data["source"]

            if "is_active" in update_data:
                skill.is_active = update_data["is_active"]

            self.repository.update_skill(skill)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to update skill '%s'.", skill_id)
            raise

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

            skill = SkillOntology(
                id=uuid4(),
                canonical_name=canonical_name,
                aliases=self._clean_aliases(request.aliases),
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

        return SkillCreateResponse.model_validate(skill)

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
        )

    def _get_skill_or_404(self, skill_id: UUID) -> SkillOntology:
        skill = self.repository.get_skill_by_id(skill_id)
        if not skill:
            raise NotFoundError(f"Skill '{skill_id}' not found.")
        return skill
