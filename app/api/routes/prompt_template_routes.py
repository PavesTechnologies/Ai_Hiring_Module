from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.prompt_template import get_prompt_template_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.prompt_template.prompt_template_request import CreatePromptRequest, UpdatePromptRequest
from app.schemas.prompt_template.prompt_template_response import (
    PromptListResponse,
    PromptLookupResponse,
    PromptResponse,
)
from app.schemas.response import APIResponse
from app.services.prompt_template_service import PromptTemplateService

router = APIRouter(prefix="/prompt-templates", tags=["Prompt Templates"])


@router.get(
    "/lookups/jd-parse",
    response_model=APIResponse[list[PromptLookupResponse]],
    status_code=status.HTTP_200_OK,
)
def get_jd_parse_prompt_lookup(
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """ACTIVE JD_PARSE prompt templates (id + name only), sorted by name - for the Job Description prompt picker."""
    prompts = service.get_jd_parse_lookup()
    return APIResponse.ok(data=prompts, message="JD parsing prompt templates retrieved successfully.")


@router.get(
    "/lookups/resume-parse",
    response_model=APIResponse[list[PromptLookupResponse]],
    status_code=status.HTTP_200_OK,
)
def get_resume_parse_prompt_lookup(
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """ACTIVE RESUME_PARSE prompt templates (id + name only), sorted by name - for the Hiring Campaign prompt picker."""
    prompts = service.get_resume_parse_lookup()
    return APIResponse.ok(data=prompts, message="Resume parsing prompt templates retrieved successfully.")


@router.get(
    "/lookups/ai-evaluate",
    response_model=APIResponse[list[PromptLookupResponse]],
    status_code=status.HTTP_200_OK,
)
def get_ai_evaluate_prompt_lookup(
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """ACTIVE AI_EVALUATE prompt templates (id + name only), sorted by name - for the Hiring Campaign ai_evaluate_prompt_id picker."""
    prompts = service.get_ai_evaluate_lookup()
    return APIResponse.ok(data=prompts, message="AI evaluation prompt templates retrieved successfully.")


@router.post(
    "",
    response_model=APIResponse[PromptResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_prompt_template(
    request: CreatePromptRequest,
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    created = service.create_prompt(
        request,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=created, message="Prompt created successfully.")


@router.get(
    "",
    response_model=APIResponse[PromptListResponse],
    status_code=status.HTTP_200_OK,
)
def list_prompt_templates(
    task_type: Optional[Literal["AI_EVALUATE", "JD_PARSE", "RESUME_PARSE"]] = Query(default=None),
    status_filter: Optional[Literal["ACTIVE", "INACTIVE"]] = Query(default=None, alias="status"),
    sort_by: Literal["created_at", "updated_at", "task_type"] = Query(default="created_at"),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.list_prompts(
        task_type=task_type,
        status=status_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )
    return APIResponse.ok(data=result, message="Prompt list retrieved successfully.")


@router.get(
    "/{prompt_id}",
    response_model=APIResponse[PromptResponse],
    status_code=status.HTTP_200_OK,
)
def get_prompt_template(
    prompt_id: UUID,
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    prompt = service.get_prompt(prompt_id)
    return APIResponse.ok(data=prompt, message="Prompt retrieved successfully.")


@router.put(
    "/{prompt_id}",
    response_model=APIResponse[PromptResponse],
    status_code=status.HTTP_200_OK,
)
def update_prompt_template(
    prompt_id: UUID,
    request: UpdatePromptRequest,
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    updated = service.update_prompt(
        prompt_id,
        request,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=updated, message="Prompt updated successfully.")


@router.delete(
    "/{prompt_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
)
def delete_prompt_template(
    prompt_id: UUID,
    service: PromptTemplateService = Depends(get_prompt_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.delete_prompt(
        prompt_id,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(message="Prompt deleted successfully.")
