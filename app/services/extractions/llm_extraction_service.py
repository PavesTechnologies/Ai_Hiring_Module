from app.schemas.ai.jd_extraction_response import JDExtractionGenerationSchema
from app.services.llm.base import LLMProvider


class LLMExtractionService:
    """
    Provider-agnostic entry point for every AI step. Callers keep the exact
    call shapes GeminiExtractionService had; which provider/model actually
    runs is decided by the LLMProvider passed in - normally
    resolve_active_provider(db), i.e. the admin's saved Settings config
    with .env Gemini as the fallback.
    """

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def extract_raw(
        self,
        normalized_text: str,
        prompt: str,
        response_schema: type = JDExtractionGenerationSchema,
    ) -> dict:
        """
        Returns the parsed JSON payload, unvalidated.
        `prompt` is always the caller's selected prompt_templates.template_text
        (JD_PARSE/RESUME_PARSE) - there is no built-in default.
        """
        full_prompt = f"""
        {prompt}

        Job Description:

        {normalized_text}
        """
        return self.provider.generate_json(full_prompt, response_schema)

    def generate_structured(self, prompt: str, response_schema: type) -> dict:
        """
        Sends an already fully-composed prompt and returns the parsed JSON
        payload, unvalidated. extract_raw's "Job Description:" framing is
        JD/Resume-extraction specific (one prompt + one normalized_text
        blob); callers that assemble their own complete prompt from more
        than one document (e.g. AI Evaluation, which needs both a JD JSON
        and a resume JSON) use this instead.
        """
        return self.provider.generate_json(prompt, response_schema)
