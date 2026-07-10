from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from google import genai
from pydantic import ValidationError
import json

from app.core.config import settings
from app.prompts.jd_extraction_prompt import SYSTEM_PROMPT
from app.schemas.ai.jd_extraction_response import JDExtractionResponse


class GeminiExtractionService:
    
    def __init__(self):
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
        )
    
    def extract(self, normalized_text: str) -> JDExtractionResponse:
        prompt = f"""
        {SYSTEM_PROMPT}
        
        Job Description: 
        
        {normalized_text}
        """
        
        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
            }
        )
        
        try:
            data = json.loads(response.text)
            return JDExtractionResponse.model_validate(
                data
            )
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Gemini returned invalid JSON: {e}"
            )
        
        except ValidationError as e:
            raise ValueError(
                f"Gemini response schema validation falied: {e}"
            )