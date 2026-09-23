from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

ProviderKey = Literal["GOOGLE", "ANTHROPIC", "OPENAI", "GROQ"]


class ProviderOptionResponse(BaseModel):
    key: ProviderKey
    label: str


class ModelOptionResponse(BaseModel):
    id: str
    display_name: str


class AIProviderConfigResponse(BaseModel):
    """
    What Settings shows. The API key itself is never returned - only its
    last 4 characters, masked.
    source="env_fallback" means no verified config is saved and processing
    is running on the .env Gemini settings.
    """
    source: Literal["database", "env_fallback"]
    provider: ProviderKey
    provider_label: str
    model_name: str
    api_key_masked: Optional[str] = None
    is_verified: Optional[bool] = None
    verified_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class _ProviderKeyRequest(BaseModel):
    provider: ProviderKey
    # Optional: omitted (or blank) means "use the key already saved for
    # this provider", so an admin can switch models without re-typing it.
    api_key: Optional[str] = Field(default=None, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None


class ListModelsRequest(_ProviderKeyRequest):
    pass


class AIProviderConfigRequest(_ProviderKeyRequest):
    """Body for both POST /verify and PUT (save)."""
    model_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        return value.strip()


class VerifyResponse(BaseModel):
    verified: bool
    message: str
    latency_ms: Optional[int] = None
