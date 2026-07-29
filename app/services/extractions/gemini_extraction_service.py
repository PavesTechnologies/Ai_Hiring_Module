from google import genai
from app.core.config import settings
import json

from app.schemas.ai.jd_extraction_response import JDExtractionGenerationSchema


class GeminiExtractionService:

    def __init__(self):
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def extract_raw(
        self,
        normalized_text: str,
        prompt: str,
        response_schema: type = JDExtractionGenerationSchema,
    ) -> dict:
        """
        Calls Gemini and returns the parsed JSON payload, unvalidated.
        `prompt` is always the caller's selected prompt_templates.template_text
        (JD_PARSE/RESUME_PARSE) - there is no built-in default.
        """
        full_prompt = f"""
        {prompt}

        Job Description:

        {normalized_text}
        """

        response = self.client.models.generate_content(
            model=settings.gemini_model,
            contents=full_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": response_schema,
            }
        )

        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Gemini returned invalid JSON: {e}"
            )