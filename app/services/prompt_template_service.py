import hashlib
import logging
from typing import Optional
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import ConflictError, NotFoundError
from app.models.prompt_template import PromptTemplate, PromptTemplateStatus
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.schemas.prompt_template.prompt_template_request import CreatePromptRequest, UpdatePromptRequest
from app.schemas.prompt_template.prompt_template_response import (
    PromptListResponse,
    PromptLookupResponse,
    PromptResponse,
)
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)


class PromptTemplateService:
    """Business logic backing the Prompt Management CRUD endpoints."""

    def __init__(self, repository: PromptTemplateRepository, audit_service: AuditService):
        self.repository = repository
        self.audit_service = audit_service

    def create_prompt(
        self,
        request: CreatePromptRequest,
        *,
        updated_by: str,
        actor_role: Optional[str],
    ) -> PromptResponse:
        try:
            content_hash = self._compute_hash(request.template_text)
            if self.repository.exists_by_hash(content_hash):
                raise ConflictError("A prompt template with identical content already exists.")

            prompt = PromptTemplate(
                task_type=request.task_type,
                name=request.name,
                template_text=request.template_text,
                content_hash=content_hash,
                status=PromptTemplateStatus(request.status),
                notes=request.notes,
                updated_by=updated_by,
            )
            self.repository.create(prompt)

            self.audit_service.log(
                actor_id=updated_by,
                actor_role=actor_role,
                action_type=ActionType.PROMPT_CREATED,
                entity_type=EntityType.PROMPT_TEMPLATE,
                entity_id=prompt.id,
                details={"task_type": prompt.task_type, "name": prompt.name, "status": prompt.status.value},
            )

            self.repository.commit()
            logger.info("Prompt template created | task_type=%s prompt_id=%s", prompt.task_type, prompt.id)
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to create prompt template for task_type '%s'.", request.task_type)
            raise

        return self._to_response(prompt)

    def update_prompt(
        self,
        prompt_id: UUID,
        request: UpdatePromptRequest,
        *,
        updated_by: str,
        actor_role: Optional[str],
    ) -> PromptResponse:
        try:
            prompt = self._get_prompt_or_404(prompt_id)
            update_data = request.model_dump(exclude_unset=True)

            before: dict = {}
            after: dict = {}
            status_change: Optional[tuple[str, str]] = None

            if "name" in update_data:
                new_name = update_data["name"]
                if new_name != prompt.name:
                    before["name"] = prompt.name
                    after["name"] = new_name
                    prompt.name = new_name

            if "template_text" in update_data:
                new_text = update_data["template_text"]
                if new_text != prompt.template_text:
                    new_hash = self._compute_hash(new_text)
                    if self.repository.exists_by_hash(new_hash, exclude_id=prompt.id):
                        raise ConflictError("A prompt template with identical content already exists.")
                    before["template_text"] = prompt.template_text
                    after["template_text"] = new_text
                    before["content_hash"] = prompt.content_hash
                    after["content_hash"] = new_hash
                    prompt.template_text = new_text
                    prompt.content_hash = new_hash

            if "notes" in update_data:
                new_notes = update_data["notes"]
                if new_notes != prompt.notes:
                    before["notes"] = prompt.notes
                    after["notes"] = new_notes
                    prompt.notes = new_notes

            if "status" in update_data:
                new_status = PromptTemplateStatus(update_data["status"])
                if new_status != prompt.status:
                    before["status"] = prompt.status.value
                    after["status"] = new_status.value
                    status_change = (prompt.status.value, new_status.value)
                    prompt.status = new_status

            if before:
                prompt.updated_by = updated_by
                self.repository.update(prompt)

                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role=actor_role,
                    action_type=ActionType.PROMPT_UPDATED,
                    entity_type=EntityType.PROMPT_TEMPLATE,
                    entity_id=prompt.id,
                    details={"before": before, "after": after},
                )

                if status_change is not None:
                    old_status, new_status_value = status_change
                    self.audit_service.log(
                        actor_id=updated_by,
                        actor_role=actor_role,
                        action_type=ActionType.PROMPT_STATUS_CHANGED,
                        entity_type=EntityType.PROMPT_TEMPLATE,
                        entity_id=prompt.id,
                        details={"old_status": old_status, "new_status": new_status_value},
                    )

            self.repository.commit()
            logger.info(
                "Prompt template updated | prompt_id=%s fields_changed=%s", prompt.id, list(before.keys())
            )
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to update prompt template '%s'.", prompt_id)
            raise

        return self._to_response(prompt)

    def get_prompt(self, prompt_id: UUID) -> PromptResponse:
        prompt = self._get_prompt_or_404(prompt_id)
        return self._to_response(prompt)

    def list_prompts(
        self,
        *,
        task_type: Optional[str],
        status: Optional[str],
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
    ) -> PromptListResponse:
        status_enum = PromptTemplateStatus(status) if status else None
        prompts = self.repository.list(
            task_type=task_type,
            status=status_enum,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
        total = self.repository.count(task_type=task_type, status=status_enum)

        return PromptListResponse(
            items=[self._to_response(prompt) for prompt in prompts],
            page=page,
            page_size=page_size,
            total=total,
        )

    def get_jd_parse_lookup(self) -> list[PromptLookupResponse]:
        """ACTIVE JD_PARSE prompts, id+name only, sorted by name - for the JD create/update prompt picker."""
        return [
            PromptLookupResponse(id=prompt.id, name=prompt.name)
            for prompt in self.repository.get_active_jd_parse_prompts()
        ]

    def get_resume_parse_lookup(self) -> list[PromptLookupResponse]:
        """ACTIVE RESUME_PARSE prompts, id+name only, sorted by name - for the Campaign create/update prompt picker."""
        return [
            PromptLookupResponse(id=prompt.id, name=prompt.name)
            for prompt in self.repository.get_active_resume_parse_prompts()
        ]

    def get_ai_evaluate_lookup(self) -> list[PromptLookupResponse]:
        """ACTIVE AI_EVALUATE prompts, id+name only, sorted by name - for the Campaign create/update ai_evaluate_prompt_id picker."""
        return [
            PromptLookupResponse(id=prompt.id, name=prompt.name)
            for prompt in self.repository.get_active_ai_evaluate_prompts()
        ]

    def delete_prompt(self, prompt_id: UUID, *, updated_by: str, actor_role: Optional[str]) -> None:
        try:
            prompt = self._get_prompt_or_404(prompt_id)
            task_type = prompt.task_type
            self.repository.delete(prompt)

            self.audit_service.log(
                actor_id=updated_by,
                actor_role=actor_role,
                action_type=ActionType.PROMPT_DELETED,
                entity_type=EntityType.PROMPT_TEMPLATE,
                entity_id=prompt_id,
                details={"task_type": task_type},
            )

            self.repository.commit()
            logger.info("Prompt template deleted | prompt_id=%s task_type=%s", prompt_id, task_type)
        except Exception:
            self.repository.rollback()
            logger.exception("Failed to delete prompt template '%s'.", prompt_id)
            raise

    @staticmethod
    def _compute_hash(template_text: str) -> str:
        return hashlib.sha256(template_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _to_response(prompt: PromptTemplate) -> PromptResponse:
        return PromptResponse(
            id=prompt.id,
            task_type=prompt.task_type,
            name=prompt.name,
            template_text=prompt.template_text,
            content_hash=prompt.content_hash,
            status=prompt.status.value,
            notes=prompt.notes,
            updated_by=prompt.updated_by,
            updated_at=prompt.updated_at,
            created_at=prompt.created_at,
        )

    def _get_prompt_or_404(self, prompt_id: UUID) -> PromptTemplate:
        prompt = self.repository.get_by_id(prompt_id)
        if not prompt:
            raise NotFoundError(f"Prompt template '{prompt_id}' not found.")
        return prompt
