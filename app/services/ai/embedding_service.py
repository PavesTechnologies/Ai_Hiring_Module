from typing import ClassVar, Optional

from sentence_transformers import SentenceTransformer

from app.core.config import settings
from app.schemas.ai.jd_extraction_response import JDExtractionResponse


class EmbeddingService:
    """
    Generates local sentence embeddings (all-MiniLM-L6-v2) — never a paid
    embedding API. The model is loaded once per worker process, not per call.
    """

    _model: ClassVar[Optional[SentenceTransformer]] = None

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.embedding_model

    def _get_model(self) -> SentenceTransformer:
        if EmbeddingService._model is None:
            EmbeddingService._model = SentenceTransformer(self.model_name)
        return EmbeddingService._model

    def generate_embedding(self, text: str) -> list[float]:
        vector = self._get_model().encode(text, normalize_embeddings=True)
        return vector.tolist()

    def generate_embeddings(self, texts: list[str], batch_size: int | None = None) -> list[list[float]]:
        """
        M08-E01 T06: batch counterpart to generate_embedding - encodes
        multiple texts in one SentenceTransformer.encode() call (chunked
        internally by sentence-transformers per batch_size), rather than
        one generate_embedding() call per text. Same singleton model /
        normalize_embeddings=True convention as generate_embedding - never
        a second embedding provider.
        """
        if not texts:
            return []
        vectors = self._get_model().encode(
            texts, batch_size=batch_size or 32, normalize_embeddings=True,
        )
        return [vector.tolist() for vector in vectors]

    @staticmethod
    def build_canonical_embedding_text(extraction: JDExtractionResponse, title: str) -> str:
        """
        Deterministic canonical text built from the validated structured JD
        JSON (not raw_text), used as the embedding input per spec.
        """
        parts = [title]

        if extraction.required_skills:
            parts.append("Required Skills: " + ", ".join(extraction.required_skills))
        if extraction.preferred_skills:
            parts.append("Preferred Skills: " + ", ".join(extraction.preferred_skills))
        if extraction.responsibilities:
            parts.append("Responsibilities: " + "; ".join(extraction.responsibilities))
        if extraction.certifications:
            parts.append("Certifications: " + ", ".join(extraction.certifications))

        if extraction.experience and (
            extraction.experience.min_experience_years is not None
            or extraction.experience.max_experience_years is not None
        ):
            min_years = extraction.experience.min_experience_years
            max_years = extraction.experience.max_experience_years
            parts.append(f"Experience: {min_years or 0}-{max_years or min_years or 0} years")

        if extraction.education and (extraction.education.degree or extraction.education.field):
            education_parts = [p for p in (extraction.education.degree, extraction.education.field) if p]
            parts.append("Education: " + " ".join(education_parts))

        if extraction.employment_type:
            parts.append(f"Employment Type: {extraction.employment_type}")
        if extraction.work_mode:
            parts.append(f"Work Mode: {extraction.work_mode}")
        if extraction.location:
            parts.append(f"Location: {extraction.location}")

        return "\n".join(parts)
