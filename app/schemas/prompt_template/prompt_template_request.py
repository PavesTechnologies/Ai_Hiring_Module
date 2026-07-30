from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TASK_TYPE_VALUES = ("AI_EVALUATE", "JD_PARSE", "RESUME_PARSE")
PROMPT_STATUS_VALUES = ("ACTIVE", "INACTIVE")


class CreatePromptRequest(BaseModel):
    """POST body for creating a new prompt template. Exactly one template per task_type."""

    task_type: Literal["AI_EVALUATE", "JD_PARSE", "RESUME_PARSE"]
    name: str = Field(..., min_length=1, max_length=150)
    template_text: str = Field(..., min_length=20, max_length=50000)
    notes: Optional[str] = Field(default=None, max_length=1000)
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"

    @field_validator("task_type", mode="before")
    @classmethod
    def _normalize_task_type(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value):
        if not value:
            raise ValueError("name cannot be empty.")
        return value

    @field_validator("template_text", mode="before")
    @classmethod
    def _strip_template_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("notes", mode="before")
    @classmethod
    def _strip_notes(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("notes")
    @classmethod
    def _blank_notes_to_none(cls, value):
        return value or None


class UpdatePromptRequest(BaseModel):
    """
    PUT body for editing a prompt template. task_type is immutable and not
    accepted here - only template_text/notes/status may change, and only
    fields present in the request are applied.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    template_text: Optional[str] = Field(default=None, min_length=20, max_length=50000)
    notes: Optional[str] = Field(default=None, max_length=1000)
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value):
        if value is not None and not value:
            raise ValueError("name cannot be empty.")
        return value

    @field_validator("template_text", mode="before")
    @classmethod
    def _strip_template_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("notes", mode="before")
    @classmethod
    def _strip_notes(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("notes")
    @classmethod
    def _blank_notes_to_none(cls, value):
        return value or None
