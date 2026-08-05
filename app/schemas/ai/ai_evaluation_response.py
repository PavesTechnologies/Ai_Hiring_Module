from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AIEvaluationScores(BaseModel):
    technical_match: int
    experience_match: int
    education_match: int
    domain_match: int
    overall_score: int

    @field_validator(
        "technical_match", "experience_match", "education_match", "domain_match", "overall_score",
    )
    @classmethod
    def validate_score_range(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("score must be between 0 and 100")
        return value


class AIEvaluationResponse(BaseModel):
    """
    Validated shape of the AI Evaluation stage's response - recommendation
    reuses the exact same vocabulary as AIRecommendation (app/models/
    pipeline.py: SHORTLIST/HOLD/REJECT), not a new one.
    """
    scores: AIEvaluationScores
    confidence_score: int
    recommendation: Literal["SHORTLIST", "HOLD", "REJECT"]
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    @field_validator("confidence_score")
    @classmethod
    def validate_confidence_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("confidence_score must be between 0 and 100")
        return value


class AIEvaluationGenerationSchema(BaseModel):
    """
    Same shape as AIEvaluationResponse, used as Gemini's structured-output
    response_schema - mirrors JDExtractionGenerationSchema/
    ResumeExtractionGenerationSchema's separation of "generation schema"
    (no validators - Gemini's Developer API mode compiles this to a JSON
    Schema itself) from "response schema" (validators applied after
    parsing).
    """
    scores: AIEvaluationScores
    confidence_score: int
    recommendation: Literal["SHORTLIST", "HOLD", "REJECT"]
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
