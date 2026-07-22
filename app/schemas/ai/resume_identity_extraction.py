from pydantic import BaseModel, field_validator


class ResumeIdentityExtraction(BaseModel):
    """
    Minimal, separate-from-content schema for the bulk-ZIP upload flow's
    identity-resolution call. ResumeExtractionResponse/
    ResumeExtractionGenerationSchema (the canonical resume-content
    extraction, shared with the single-upload path) are deliberately
    PII-free — full_name/email/phone must never appear in resumes.parsed_json.
    Bulk uploads have no upload form to source candidate identity from
    otherwise, so a second, narrowly-scoped Gemini call using this schema
    resolves identity only for Candidate creation; its output is never
    merged into parsed_json.
    """

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None

    @field_validator("full_name", "email", "phone")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
