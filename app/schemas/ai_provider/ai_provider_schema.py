from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

ProviderKey = Literal["GOOGLE", "ANTHROPIC", "OPENAI", "GROQ"]


class ProviderOptionResponse(BaseModel):
    key: ProviderKey
    label: str
    # True once this provider has a row - the "Add" modal only offers the rest.
    registered: bool


class ModelOptionResponse(BaseModel):
    id: str
    display_name: str


class ActiveProviderResponse(BaseModel):
    """
    What processing uses right now. source="env_fallback" means no provider
    is registered yet and the .env Gemini settings are in use.
    """
    source: Literal["database", "env_fallback"]
    provider: ProviderKey
    provider_label: str
    model_name: str


class AIProviderRowResponse(BaseModel):
    """One table row. The API key is never returned - only its last 4 characters, masked."""
    id: UUID
    provider: ProviderKey
    provider_label: str
    model_name: str
    api_key_masked: Optional[str] = None
    is_active: bool
    is_verified: bool
    verified_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AIProviderListResponse(BaseModel):
    items: list[AIProviderRowResponse]
    page: int
    page_size: int
    total: int


class _ProviderKeyRequest(BaseModel):
    provider: ProviderKey
    # Optional: omitted (or blank) means "use the key already saved for this
    # provider", so an admin can switch models without re-typing it.
    api_key: Optional[str] = Field(default=None, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _normalize_key(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None


class ListModelsRequest(_ProviderKeyRequest):
    pass


class VerifyRequest(_ProviderKeyRequest):
    """Body for POST /verify - the Check button."""
    model_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Select a model.")
        return value


class CreateAIProviderRequest(VerifyRequest):
    """Register a provider. The key is required - there's nothing saved to reuse yet."""
    api_key: str = Field(..., max_length=500)

    @field_validator("api_key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:  # overrides the optional-key version
        value = value.strip()
        if not value:
            raise ValueError("Enter an API key.")
        return value


class UpdateAIProviderRequest(BaseModel):
    """Edit a registered provider. The provider itself is fixed; a blank key keeps the saved one."""
    model_name: str = Field(..., min_length=1, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _normalize_key(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Select a model.")
        return value


class VerifyResponse(BaseModel):
    verified: bool
    message: str
    latency_ms: Optional[int] = None
