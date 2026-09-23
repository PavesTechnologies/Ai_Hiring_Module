from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.ai_provider_config import get_ai_provider_config_service
from app.middleware.rbac import TokenUser, require_roles, resolve_actor_role
from app.models.identity import UserRole
from app.schemas.ai_provider.ai_provider_schema import (
    ActiveProviderResponse,
    AIProviderListResponse,
    AIProviderRowResponse,
    CreateAIProviderRequest,
    ListModelsRequest,
    ModelOptionResponse,
    ProviderOptionResponse,
    UpdateAIProviderRequest,
    VerifyRequest,
    VerifyResponse,
)
from app.schemas.response import APIResponse
from app.services.ai_provider_config_service import AIProviderConfigService

router = APIRouter(prefix="/ai-providers", tags=["AI Providers"])


@router.get(
    "/options",
    response_model=APIResponse[list[ProviderOptionResponse]],
    status_code=status.HTTP_200_OK,
)
def list_provider_options(
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """All supported providers, flagged with whether each is already registered."""
    return APIResponse.ok(data=service.list_provider_options(), message="AI providers retrieved successfully.")


@router.get(
    "",
    response_model=APIResponse[AIProviderListResponse],
    status_code=status.HTTP_200_OK,
)
def list_registered_providers(
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=50),
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """The Settings table - registered providers, active first. Keys are masked."""
    return APIResponse.ok(
        data=service.list_registered(page=page, page_size=page_size),
        message="Registered AI providers retrieved successfully.",
    )


@router.get(
    "/active",
    response_model=APIResponse[ActiveProviderResponse],
    status_code=status.HTTP_200_OK,
)
def get_active_provider(
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """What processing uses right now - the active provider, or the .env fallback when none is registered."""
    return APIResponse.ok(data=service.get_active_provider(), message="Active AI provider retrieved successfully.")


@router.post(
    "/models",
    response_model=APIResponse[list[ModelOptionResponse]],
    status_code=status.HTTP_200_OK,
)
def list_models(
    request: ListModelsRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Models available to the given key, fetched live from the provider. POST so the key isn't in a URL."""
    return APIResponse.ok(data=service.list_models(request), message="Models retrieved successfully.")


@router.post(
    "/verify",
    response_model=APIResponse[VerifyResponse],
    status_code=status.HTTP_200_OK,
)
def verify_provider(
    request: VerifyRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """The "Check" button: one test call with these values. Saves nothing."""
    result = service.verify(request)
    return APIResponse.ok(data=result, message=result.message)


@router.post(
    "",
    response_model=APIResponse[AIProviderRowResponse],
    status_code=status.HTTP_201_CREATED,
)
def register_provider(
    request: CreateAIProviderRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Registers a provider after verifying it server-side. 409 if it's already registered."""
    row = service.create(request, updated_by=user.user_id, actor_role=resolve_actor_role(user))
    message = (
        f"{row.provider_label} registered and set as the active provider."
        if row.is_active else f"{row.provider_label} registered."
    )
    return APIResponse.ok(data=row, message=message)


@router.put(
    "/{config_id}",
    response_model=APIResponse[AIProviderRowResponse],
    status_code=status.HTTP_200_OK,
)
def update_provider(
    config_id: UUID,
    request: UpdateAIProviderRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Changes the model and optionally the key, after verifying again server-side."""
    row = service.update(config_id, request, updated_by=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=row, message=f"{row.provider_label} updated.")


@router.post(
    "/{config_id}/activate",
    response_model=APIResponse[AIProviderRowResponse],
    status_code=status.HTTP_200_OK,
)
def activate_provider(
    config_id: UUID,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Makes this provider the one all processing uses."""
    row = service.activate(config_id, updated_by=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=row, message=f"{row.provider_label} is now the active provider.")


@router.delete(
    "/{config_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
)
def delete_provider(
    config_id: UUID,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Deletes a provider. 409 for the active provider or the last one remaining."""
    service.delete(config_id, updated_by=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(message="AI provider deleted.")
