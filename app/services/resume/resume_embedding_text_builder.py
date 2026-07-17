from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse


def build_canonical_embedding_text(extraction: ResumeExtractionResponse) -> str:
    """
    Deterministic canonical text built from the validated structured Resume
    JSON (not raw_text), used as the embedding input — same
    field-concatenation style as EmbeddingService.build_canonical_embedding_text,
    kept as a standalone function here (rather than a new method on
    EmbeddingService) since the fields it draws from are Resume-only.
    metadata is deliberately excluded, same reasoning as the JD builder.
    """
    parts = []

    if extraction.skills:
        parts.append("Skills: " + ", ".join(extraction.skills))

    for entry in extraction.work_experience:
        segment_parts = []
        if entry.title:
            segment_parts.append(entry.title)
        if entry.company:
            segment_parts.append(f"at {entry.company}")
        header = " ".join(segment_parts)

        dates = [d for d in (entry.start_date, entry.end_date) if d]
        date_range = "-".join(dates) if dates else None

        segment = header
        if date_range:
            segment = f"{segment} ({date_range})" if segment else date_range
        if entry.description:
            segment = f"{segment}: {entry.description}" if segment else entry.description

        if segment:
            parts.append("Work Experience: " + segment)

    for education in extraction.education:
        education_parts = [p for p in (education.degree, education.field, education.institution) if p]
        if education.graduation_year is not None:
            education_parts.append(str(education.graduation_year))
        if education_parts:
            parts.append("Education: " + " ".join(education_parts))

    if extraction.certifications:
        parts.append("Certifications: " + ", ".join(extraction.certifications))

    if extraction.total_experience_years is not None:
        parts.append(f"Total Experience: {extraction.total_experience_years} years")

    if extraction.summary:
        parts.append(f"Summary: {extraction.summary}")

    return "\n".join(parts)
