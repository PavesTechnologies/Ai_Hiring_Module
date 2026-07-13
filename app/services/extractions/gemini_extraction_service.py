from google import genai
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.core.config import settings
from pydantic import ValidationError
import json

from app.core.config import settings
from app.prompts.jd_extraction_prompt import SYSTEM_PROMPT
from app.schemas.ai.jd_extraction_response import JDExtractionResponse


class GeminiExtractionService:

    def __init__(self):
        self.client = genai.Client(api_key=settings.gemini_api_key)
    
    def extract_raw(self, normalized_text: str) -> dict:
        """
        Calls Gemini and returns the parsed JSON payload, unvalidated.
        Kept separate from extract() so a pipeline can track "call the AI"
        and "validate its output" as two distinct stages.
        """
        prompt = f"""
        {SYSTEM_PROMPT}

        Job Description:

        {normalized_text}
        """

        response = self.client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
            }
        )

        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Gemini returned invalid JSON: {e}"
            )

    def extract(self, normalized_text: str) -> JDExtractionResponse:
        data = self.extract_raw(normalized_text)

        try:
            return JDExtractionResponse.model_validate(
                data
            )
        except ValidationError as e:
            raise ValueError(
                f"Gemini response schema validation falied: {e}"
            )