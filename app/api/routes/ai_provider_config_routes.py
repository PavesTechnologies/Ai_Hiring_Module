from fastapi import APIRouter, Depends, Security, status

from app.dependencies.ai_provider_config import get_ai_provider_config_service
from app.middleware.rbac import TokenUser, require_roles, resolve_actor_role
from app.models.identity import UserRole
from app.schemas.ai_provider.ai_provider_schema import (
    AIProviderConfigRequest,
    AIProviderConfigResponse,
    ListModelsRequest,
    ModelOptionResponse,
    ProviderOptionResponse,
    VerifyResponse,
)
from app.schemas.response import APIResponse
from app.services.ai_provider_config_service import AIProviderConfigService

router = APIRouter(prefix="/ai-provider-config", tags=["AI Provider Config"])


@router.get(
    "/providers",
    response_model=APIResponse[list[ProviderOptionResponse]],
    status_code=status.HTTP_200_OK,
)
def list_providers(
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Providers the Settings dropdown offers."""
    return APIResponse.ok(data=AIProviderConfigService.list_providers(), message="AI providers retrieved successfully.")


@router.get(
    "",
    response_model=APIResponse[AIProviderConfigResponse],
    status_code=status.HTTP_200_OK,
)
def get_ai_provider_config(
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """The provider/model processing currently uses - the saved config, or the .env fallback. Never returns the key."""
    return APIResponse.ok(data=service.get_current(), message="AI provider configuration retrieved successfully.")


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
def verify_ai_provider_config(
    request: AIProviderConfigRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """The Settings "Check" button: one test call with these values. Saves nothing."""
    result = service.verify(request)
    return APIResponse.ok(data=result, message=result.message)


@router.put(
    "",
    response_model=APIResponse[AIProviderConfigResponse],
    status_code=status.HTTP_200_OK,
)
def save_ai_provider_config(
    request: AIProviderConfigRequest,
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Verifies again server-side, then saves. 422 if the model can't be used."""
    saved = service.save(request, updated_by=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=saved, message="AI provider saved. New processing jobs will use this model.")


@router.delete(
    "",
    response_model=APIResponse[AIProviderConfigResponse],
    status_code=status.HTTP_200_OK,
)
def reset_ai_provider_config(
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """Removes the saved config; processing falls back to the .env Gemini settings."""
    current = service.reset_to_default(updated_by=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=current, message="AI provider reset to the built-in default.")
