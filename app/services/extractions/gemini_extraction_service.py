from google import genai
from google.genai import types
from app.core.config import settings
import json

from app.schemas.ai.jd_extraction_response import JDExtractionGenerationSchema

# Status codes retried inside the SDK: the provider being unavailable or
# overloaded. Re-sending the same request shortly after is the correct
# response to these, and they describe the request, not the document.
#
# 429 is deliberately NOT here. Gemini returns 429 for two very different
# things and the HTTP layer cannot tell them apart: short-term rate
# limiting (worth retrying) and RESOURCE_EXHAUSTED quota/billing
# exhaustion (retrying cannot succeed, and each attempt spends another
# call against a quota that is already gone). Since quota exhaustion is
# the failure this project actually hits, 429 is handled one level up in
# error_classifier.classify, which can read the message and tell the two
# apart.
_RETRYABLE_STATUS_CODES = [500, 502, 503, 504]


def _build_http_options() -> types.HttpOptions:
    """
    Transport-level retry + timeout for every Gemini call.

    This is the layer a 503 belongs at. Without it the only retry in the
    system was retry_policy/RetryDriver, which re-runs whole pipeline
    stages: one 503 in AI_EXTRACTION cost a fresh TEXT_EXTRACTION,
    TEXT_CLEANING, PII_DETECTION and PII_REDACTION (observed live — 5 AI
    attempts produced 25 stage-execution rows, and PII detection ran on
    the same document five times), with 10-120s of pipeline backoff for a
    blip that clears in seconds. Retrying here re-sends only the failed
    request and redoes nothing else.

    RetryDriver is still the backstop for a genuine outage that outlasts
    these attempts — the two layers are complementary, not redundant.
    """
    return types.HttpOptions(
        timeout=settings.gemini_timeout_ms,
        retry_options=types.HttpRetryOptions(
            attempts=settings.gemini_retry_attempts,
            initial_delay=settings.gemini_retry_initial_delay,
            max_delay=settings.gemini_retry_max_delay,
            exp_base=settings.gemini_retry_exp_base,
            jitter=settings.gemini_retry_jitter,
            http_status_codes=_RETRYABLE_STATUS_CODES,
        ),
    )


class GeminiExtractionService:

    def __init__(self):
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=_build_http_options(),
        )

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

    def generate_structured(self, prompt: str, response_schema: type) -> dict:
        """
        Calls Gemini with an already fully-composed prompt and returns the
        parsed JSON payload, unvalidated. extract_raw's "Job Description:"
        framing is JD/Resume-extraction specific (one prompt + one
        normalized_text blob); callers that assemble their own complete
        prompt from more than one document (e.g. AI Evaluation, which needs
        both a JD JSON and a resume JSON) use this instead, so extract_raw
        and its existing callers stay untouched.
        """
        response = self.client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
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