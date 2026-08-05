"""
M08-E01 S02 Phase 1: builds the JD embedding input text from already-
persisted, post-skill-normalization data - jd_skills joined to
skill_ontology for canonical_name (never the raw, pre-normalization skill
strings), plus the JD's own title/raw_text.

Distinct from EmbeddingService.build_canonical_embedding_text, which
builds from the in-memory, pre-normalization JDExtractionResponse used by
the existing inline EMBEDDING_GENERATION pipeline stage
(app/services/jd/jd_processing_pipeline.py). That existing builder/stage
is left completely untouched - this is an additional, separate utility
for a different (not-yet-wired-to-Celery) generation path, so the two can
coexist without any behavior change to the existing one.

Never reads candidate/resume data - JD-only inputs (title, jd_skills'
canonical names, raw_text).
"""


def build_jd_embedding_text(
    title: str,
    raw_text: str,
    mandatory_skill_names: list[str],
    preferred_skill_names: list[str],
    max_chars: int,
) -> str:
    """
    {title}.
    Required skills: {comma-separated canonical_names of mandatory jd_skills}.
    Preferred skills: {comma-separated canonical_names of preferred jd_skills}.
    {raw_text truncated to max_chars}

    Only raw_text is truncated - title and the skill lists are never cut.
    """
    required_line = f"Required skills: {', '.join(mandatory_skill_names)}."
    preferred_line = f"Preferred skills: {', '.join(preferred_skill_names)}."
    truncated_raw_text = (raw_text or "")[:max_chars]

    return f"{title}.\n{required_line}\n{preferred_line}\n{truncated_raw_text}"
