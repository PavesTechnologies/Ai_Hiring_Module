import json

from google import genai
from pydantic import ValidationError

from app.core.config import settings
from app.prompts.resume_extraction_prompt import SYSTEM_PROMPT
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse


class GeminiResumeExtractionService:
    """
    Resume-specific counterpart to GeminiExtractionService (JD's). Kept as
    its own class rather than a shared base — the two prompts/schemas are
    genuinely different documents, same rationale JDProcessingPipeline's
    docstring gives for not sharing a base pipeline class.
    """

    def __init__(self):
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def extract_raw(self, normalized_text: str) -> dict:
        prompt = f"""
        {SYSTEM_PROMPT}

        Resume:

        {normalized_text}
        """

        response = self.client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
            },
        )

        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Gemini returned invalid JSON: {e}")

    def extract(self, normalized_text: str) -> ResumeExtractionResponse:
        data = self.extract_raw(normalized_text)

        try:
            return ResumeExtractionResponse.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"Gemini response schema validation failed: {e}")
