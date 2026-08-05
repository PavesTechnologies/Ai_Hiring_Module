import json

from app.schemas.ai.ai_evaluation_response import AIEvaluationGenerationSchema, AIEvaluationResponse
from app.services.extractions.gemini_extraction_service import GeminiExtractionService


class AIEvaluationService:
    """
    Terminal screening stage: independently evaluates a candidate against a
    job using ONLY the already-extracted Resume JSON and Job Description
    JSON - never raw resume/JD text, never the deterministic/semantic
    scores or explanations those (independent) layers already computed.
    Mirrors SemanticScoringService's shape (injected dependency, one public
    method, returns a plain dict) but persists nothing - that belongs to a
    future phase.
    """

    def __init__(self, extraction_service: GeminiExtractionService):
        self.extraction_service = extraction_service

    def evaluate_candidate(
        self,
        resume_json: dict,
        jd_json: dict,
        prompt_template_text: str,
    ) -> dict:
        rendered_prompt = self._render_prompt(prompt_template_text, resume_json, jd_json)
        raw_response = self.extraction_service.generate_structured(
            prompt=rendered_prompt,
            response_schema=AIEvaluationGenerationSchema,
        )
        try:
            validated = AIEvaluationResponse.model_validate(raw_response)
        except Exception as exc:
            # Normalized to ValueError regardless of the validator's own
            # exception type, so error_classifier.classify() reliably marks
            # a malformed AI response PERMANENT (matches extract_raw's own
            # json.JSONDecodeError -> ValueError convention).
            raise ValueError(f"AI Evaluation response failed schema validation: {exc}") from exc

        return validated.model_dump()

    @staticmethod
    def _render_prompt(prompt_template_text: str, resume_json: dict, jd_json: dict) -> str:
        """
        Plain string assembly - the Prompt Template module has no
        placeholder/Jinja templating (see GeminiExtractionService.
        extract_raw's own concatenation for the same convention elsewhere
        in this codebase). Both documents are the already AI-ready
        structured JSON produced by earlier pipeline stages.
        """
        return (
            f"{prompt_template_text}\n\n"
            f"Job Description (JSON):\n{json.dumps(jd_json)}\n\n"
            f"Candidate Resume (JSON):\n{json.dumps(resume_json)}"
        )
