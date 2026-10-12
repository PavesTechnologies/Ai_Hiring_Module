from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ProviderKey = Literal["GOOGLE", "ANTHROPIC", "OPENAI", "GROQ"]
# What a model is used for - see app.services.llm.model_selection.
ModelUsageKey = Literal["EXTRACTION", "EVALUATION", "BOTH"]
_MAX_MODELS = 3


class ModelAssignmentRequest(BaseModel):
    usage: ModelUsageKey
    model_name: str = Field(..., min_length=1, max_length=200)
    # Same provider and API key; takes over when the main model is unavailable.
    fallback_model_name: Optional[str] = Field(default=None, max_length=200)

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Select a model.")
        return value

    @field_validator("fallback_model_name")
    @classmethod
    def _strip_fallback(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None


class ModelAssignmentResponse(BaseModel):
    usage: ModelUsageKey
    usage_label: str
    model_name: str
    fallback_model_name: Optional[str] = None


class _ModelsRequest(BaseModel):
    """
    The models a provider uses: `models` (one per purpose, each with an
    optional backup), or - for older clients - a single `model_name`, which
    means one model for Both. Cross-model rules (duplicates, coverage) are
    checked by the service so they come back as a plain-English 400.
    """
    model_name: Optional[str] = Field(default=None, max_length=200)
    models: Optional[list[ModelAssignmentRequest]] = Field(default=None, max_length=_MAX_MODELS)

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None

    @model_validator(mode="after")
    def _require_a_model(self):
        if not self.models and not self.model_name:
            raise ValueError("Select a model.")
        return self

    def assignments(self) -> list[ModelAssignmentRequest]:
        if self.models:
            return list(self.models)
        return [ModelAssignmentRequest(usage="BOTH", model_name=self.model_name)]


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
    models: list[ModelAssignmentResponse] = []


class AIProviderRowResponse(BaseModel):
    """One table row. The API key is never returned - only its last 4 characters, masked."""
    id: UUID
    provider: ProviderKey
    provider_label: str
    model_name: str                   # the extraction model - kept for older clients
    models: list[ModelAssignmentResponse] = []
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


class VerifyRequest(_ModelsRequest, _ProviderKeyRequest):
    """Body for POST /verify - the Check button. Checks every model (main + backup) in the setup."""


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


class UpdateAIProviderRequest(_ModelsRequest):
    """Edit a registered provider. The provider itself is fixed; a blank key keeps the saved one."""
    api_key: Optional[str] = Field(default=None, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _normalize_key(cls, value: Optional[str]) -> Optional[str]:
        value = value.strip() if value else value
        return value or None


class ModelCheckResult(BaseModel):
    model_name: str
    verified: bool
    message: str
    latency_ms: Optional[int] = None
    error_code: Optional[str] = None


class VerifyResponse(BaseModel):
    verified: bool
    message: str
    latency_ms: Optional[int] = None
    # Set when verified is False - a stable LLMErrorReason code
    # (e.g. RATE_LIMITED, DAILY_LIMIT, CREDITS_EXHAUSTED, INVALID_KEY), or
    # INVALID_MODEL_SETUP when the model assignments themselves are invalid.
    error_code: Optional[str] = None
    # One entry per distinct model checked (main and backup models).
    results: list[ModelCheckResult] = []
