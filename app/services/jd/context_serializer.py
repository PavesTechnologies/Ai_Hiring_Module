from uuid import UUID

from app.models.async_tasks import DocumentType
from app.models.jd.job_descriptions import JDSourceFormat
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.services.jd.jd_processing_context import JDProcessingContext
from app.services.skills.skill_normalization_service import SkillMatchResult, SkillMatchTier


def to_dict(context: JDProcessingContext) -> dict:
    return {
        "task_id": context.task_id,
        "title": context.title,
        "jurisdiction": context.jurisdiction,
        "min_experience_years": context.min_experience_years,
        "education_criteria": context.education_criteria,
        "created_by": context.created_by,
        "file_path": context.file_path,
        "raw_text": context.raw_text,
        "document_type": context.document_type.value if context.document_type is not None else None,
        "source_format": context.source_format.value if context.source_format is not None else None,
        "text": context.text,
        "cleaned_text": context.cleaned_text,
        "raw_extraction": context.raw_extraction,
        "extraction": context.extraction.model_dump() if context.extraction is not None else None,
        "skill_matches": [
            {
                "raw_text": match.raw_text,
                "mandatory": match.mandatory,
                "canonical_skill_id": str(match.canonical_skill_id) if match.canonical_skill_id is not None else None,
                "match_tier": match.match_tier.value if match.match_tier is not None else None,
                "confidence": match.confidence,
            }
            for match in (context.skill_matches or [])
        ],
        "content_hash": context.content_hash,
        "embedding_text": context.embedding_text,
        "embedding": context.embedding,
        "embedding_model_version_id": str(context.embedding_model_version_id) if context.embedding_model_version_id is not None else None,
        "input_text_hash": context.input_text_hash,
        "jd_id": str(context.jd_id) if context.jd_id is not None else None,
    }


def from_dict(data: dict) -> JDProcessingContext:
    context = JDProcessingContext(
        task_id=data["task_id"],
        title=data["title"],
        jurisdiction=data["jurisdiction"],
        min_experience_years=data.get("min_experience_years"),
        education_criteria=data.get("education_criteria"),
        created_by=data["created_by"],
        file_path=data.get("file_path"),
        raw_text=data.get("raw_text"),
        document_type=DocumentType(data["document_type"]) if data.get("document_type") is not None else DocumentType.JD,
    )
    context.source_format = JDSourceFormat(data["source_format"]) if data.get("source_format") is not None else None
    context.text = data.get("text")
    context.cleaned_text = data.get("cleaned_text")
    context.raw_extraction = data.get("raw_extraction")
    context.extraction = JDExtractionResponse.model_validate(data["extraction"]) if data.get("extraction") is not None else None
    context.skill_matches = [
        SkillMatchResult(
            raw_text=match["raw_text"],
            mandatory=match["mandatory"],
            canonical_skill_id=UUID(match["canonical_skill_id"]) if match.get("canonical_skill_id") is not None else None,
            match_tier=SkillMatchTier(match["match_tier"]),
            confidence=match.get("confidence"),
        )
        for match in (data.get("skill_matches") or [])
    ]
    context.content_hash = data.get("content_hash")
    context.embedding_text = data.get("embedding_text")
    context.embedding = data.get("embedding")
    context.embedding_model_version_id = UUID(data["embedding_model_version_id"]) if data.get("embedding_model_version_id") is not None else None
    context.input_text_hash = data.get("input_text_hash")
    context.jd_id = UUID(data["jd_id"]) if data.get("jd_id") is not None else None
    return context
