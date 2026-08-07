import json
from datetime import datetime, timezone
from uuid import UUID

from app.models.pipeline import AIEvaluationStatus, AIRecommendation
from app.repositories.campaign_candidate_ai_evaluation_repository import CampaignCandidateAIEvaluationRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.schemas.ai.ai_evaluation_response import AIEvaluationGenerationSchema, AIEvaluationResponse
from app.services.extractions.gemini_extraction_service import GeminiExtractionService


class AIEvaluationService:
    """
    Terminal screening stage: independently evaluates a candidate against a
    job using ONLY the already-extracted Resume JSON and Job Description
    JSON - never raw resume/JD text, never the deterministic/semantic
    scores or explanations those (independent) layers already computed.
    Mirrors CandidateScoringService/SemanticScoringService's shape exactly:
    takes an injected CampaignCandidateRepository, mutates the ORM object
    directly, flushes via repository.update() - never commits, that belongs
    to the caller (the Celery task). AI output itself lives on the related
    CampaignCandidateAIEvaluation row (1:1), created lazily on first
    evaluation via CampaignCandidateAIEvaluationRepository.get_or_create.
    """

    def __init__(
        self,
        extraction_service: GeminiExtractionService,
        campaign_candidate_repository: CampaignCandidateRepository,
        campaign_candidate_ai_evaluation_repository: CampaignCandidateAIEvaluationRepository,
    ):
        self.extraction_service = extraction_service
        self.campaign_candidate_repository = campaign_candidate_repository
        self.campaign_candidate_ai_evaluation_repository = campaign_candidate_ai_evaluation_repository

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

    def calculate_and_store_evaluation(
        self,
        campaign_candidate_id: UUID,
        resume_json: dict,
        jd_json: dict,
        prompt_template_text: str,
    ) -> dict:
        """
        Phase 2.4: same "fetch by id via injected repository, mutate the
        ORM object, flush via repository.update()" shape as
        CandidateScoringService.calculate_and_store_score_breakdown /
        SemanticScoringService.calculate_and_store_semantic_score_breakdown.
        The PASS/REJECT business decision (CandidateRejection, stage
        transition, audit log, commit) stays with the caller, exactly like
        those two services never make that decision themselves.
        """
        campaign_candidate = self.campaign_candidate_repository.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise ValueError(f"CampaignCandidate '{campaign_candidate_id}' not found.")

        ai_response = self.evaluate_candidate(resume_json, jd_json, prompt_template_text)

        scores = ai_response["scores"]
        now = datetime.now(timezone.utc)

        ai_evaluation = self.campaign_candidate_ai_evaluation_repository.get_or_create(campaign_candidate.id)

        # overall_score is already 0-100 on a Numeric(5,2) column - same
        # scale as deterministic_score, no normalization needed.
        ai_evaluation.effective_ai_score = scores["overall_score"]
        # confidence_score is a 0-100 int from the AI response; ai_confidence
        # is Numeric(5,4) (0-1 scale, same convention as semantic_score), so
        # it's normalized down to 0-1 here - a raw 0-100 value wouldn't fit
        # that column's precision.
        ai_evaluation.ai_confidence = ai_response["confidence_score"] / 100
        ai_evaluation.ai_recommendation = AIRecommendation(ai_response["recommendation"])
        ai_evaluation.ai_strengths = ai_response["strengths"]
        ai_evaluation.ai_weaknesses = ai_response["gaps"]
        # Phase 2.4 enhancement: the complete validated response, stored
        # verbatim (no reshaping/renaming) alongside the individual columns
        # above, for auditability/debugging/future re-evaluation comparison.
        ai_evaluation.ai_response_json = ai_response
        ai_evaluation.ai_evaluation_status = AIEvaluationStatus.COMPLETED
        ai_evaluation.updated_at = now
        self.campaign_candidate_ai_evaluation_repository.update(ai_evaluation)

        campaign_candidate.updated_at = now
        self.campaign_candidate_repository.update(campaign_candidate)

        return ai_response

    @staticmethod
    def build_rejection_reason(ai_response: dict) -> str:
        """
        Mirrors CandidateScoringService.build_rejection_reason's role for
        this layer - the AI's own "gaps" list is the rejection explanation,
        there's no separate breakdown to summarize.
        """
        gaps = ai_response.get("gaps") or []
        if gaps:
            return "AI evaluation identified the following gaps: " + "; ".join(gaps)
        return "AI evaluation recommended rejection."

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
