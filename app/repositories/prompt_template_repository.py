from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.prompt_template import PromptTemplate, PromptTemplateStatus

_SORT_COLUMNS = {
    "created_at": PromptTemplate.created_at,
    "updated_at": PromptTemplate.updated_at,
    "task_type": PromptTemplate.task_type,
}


class PromptTemplateRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, prompt: PromptTemplate) -> PromptTemplate:
        self.db.add(prompt)
        self.db.flush()
        self.db.refresh(prompt)
        return prompt

    def update(self, prompt: PromptTemplate) -> PromptTemplate:
        self.db.flush()
        self.db.refresh(prompt)
        return prompt

    def delete(self, prompt: PromptTemplate) -> None:
        self.db.delete(prompt)
        self.db.flush()

    def get_by_id(self, prompt_id: UUID) -> Optional[PromptTemplate]:
        return self.db.get(PromptTemplate, prompt_id)

    def get_by_task_type(self, task_type: str) -> Optional[PromptTemplate]:
        stmt = select(PromptTemplate).where(PromptTemplate.task_type == task_type)
        return self.db.execute(stmt).scalars().first()

    def get_active_by_task_type(self, task_type: str) -> list[PromptTemplate]:
        """ACTIVE prompt templates for a given task_type, sorted by name — backs the dropdown lookups."""
        stmt = (
            select(PromptTemplate)
            .where(
                PromptTemplate.task_type == task_type,
                PromptTemplate.status == PromptTemplateStatus.ACTIVE,
            )
            .order_by(PromptTemplate.name.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_active_jd_parse_prompts(self) -> list[PromptTemplate]:
        return self.get_active_by_task_type("JD_PARSE")

    def get_active_resume_parse_prompts(self) -> list[PromptTemplate]:
        return self.get_active_by_task_type("RESUME_PARSE")

    def get_active_ai_evaluate_prompts(self) -> list[PromptTemplate]:
        return self.get_active_by_task_type("AI_EVALUATE")

    def get_names_by_ids(self, prompt_template_ids: list[UUID]) -> dict[UUID, str]:
        """Batch id->name lookup - avoids one query per row when annotating a JD/Campaign list with prompt_name."""
        if not prompt_template_ids:
            return {}
        stmt = select(PromptTemplate.id, PromptTemplate.name).where(PromptTemplate.id.in_(set(prompt_template_ids)))
        return {row.id: row.name for row in self.db.execute(stmt)}

    def exists_by_hash(self, content_hash: str, *, exclude_id: Optional[UUID] = None) -> bool:
        stmt = select(PromptTemplate.id).where(PromptTemplate.content_hash == content_hash)
        if exclude_id is not None:
            stmt = stmt.where(PromptTemplate.id != exclude_id)
        return self.db.execute(stmt).first() is not None

    def _apply_filters(self, stmt, *, task_type: Optional[str], status: Optional[PromptTemplateStatus]):
        if task_type:
            stmt = stmt.where(PromptTemplate.task_type == task_type)
        if status:
            stmt = stmt.where(PromptTemplate.status == status)
        return stmt

    def list(
        self,
        *,
        task_type: Optional[str] = None,
        status: Optional[PromptTemplateStatus] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> list[PromptTemplate]:
        stmt = self._apply_filters(select(PromptTemplate), task_type=task_type, status=status)
        sort_column = _SORT_COLUMNS.get(sort_by, PromptTemplate.created_at)
        order = sort_column.asc() if sort_order == "asc" else sort_column.desc()
        stmt = stmt.order_by(order).offset((page - 1) * page_size).limit(page_size)
        return list(self.db.execute(stmt).scalars().all())

    def count(self, *, task_type: Optional[str] = None, status: Optional[PromptTemplateStatus] = None) -> int:
        stmt = self._apply_filters(select(func.count(PromptTemplate.id)), task_type=task_type, status=status)
        return self.db.execute(stmt).scalar() or 0

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
