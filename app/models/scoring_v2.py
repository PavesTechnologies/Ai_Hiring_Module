"""
Scoring v2 (Phase 1, shadow mode) - skill detection and scoring storage.

None of these tables is read by v1 scoring: v1 keeps using candidate_skills
and campaign_candidates.deterministic_breakdown exactly as before. v2 reads
resume_skill_evidence (+ core_skill_confirmations) and writes its results to
candidate_scoring_results, so both versions can run side by side.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SkillSense(Base):
    """
    One meaning of an ambiguous catalogue skill - e.g. Change Management:
    ITSM (change control: CAB, release, incident/problem) vs Organisational
    (people side of change: adoption, stakeholders). The sense of an
    occurrence is chosen from cue words near it (SkillSenseResolver). A
    skill with no rows here has no senses.
    """

    __tablename__ = "skill_senses"
    __table_args__ = (UniqueConstraint("canonical_skill_id", "sense_key", name="uq_skill_senses_skill_sense"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=False, index=True,
    )
    sense_key: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    # One sentence, shown to the confirming LLM ("Change Management in the
    # ITSM sense: controlling changes to IT services ...").
    description: Mapped[str] = mapped_column(Text, nullable=False)
    cue_words = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    # Used when nothing near a JD occurrence decides the sense - recorded
    # as "assumed", never silently.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # How many lines either side of the occurrence are searched for cues.
    window_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillScanTerm(Base):
    """
    Catalogue scan policy for an ambiguous term (canonical name or alias):
    EXCLUDE - never matched by the text scan (the LLM may still report it);
    REQUIRE_CUE - matched only when one of cue_words is near it;
    ALLOW - matched anywhere, overriding the default short-term rule.
    Keyed by the lower-cased term, independent of which skill it names.
    """

    __tablename__ = "skill_scan_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    term_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    policy: Mapped[str] = mapped_column(String(20), nullable=False)
    cue_words = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResumeSkillEvidence(Base):
    """
    v2's reading of one catalogue skill on one resume: the union of the
    deterministic text scan and the LLM extraction (the LLM can add skills,
    never remove a scanned one). One row per (resume, skill, sense) -
    replaced as a whole whenever the resume is detected again.
    """

    __tablename__ = "resume_skill_evidence"
    __table_args__ = (Index("ix_resume_skill_evidence_resume_id", "resume_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    canonical_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=False,
    )
    # skill_senses.sense_key of this occurrence; NULL when the skill has no
    # senses, or when nothing near it decided one (sense_resolved False).
    sense: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sense_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Every source that found it: SCAN / LLM / DERIVED / INFERRED.
    sources = mapped_column(ARRAY(String(20)), nullable=False)
    # The strongest of them - decides recognition.
    best_source: Mapped[str] = mapped_column(String(20), nullable=False)
    match_tier: Mapped[str] = mapped_column(String(30), nullable=False)
    recognition: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    # [{"text": line, "source": ..., "origin": ...}] - the resume lines that
    # show it, kept short; also what the core confirmation is shown.
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # The parse (LLM extraction) this detection was built from - set on
    # every row, scan-only ones included, since the union depends on it.
    parse_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    parse_model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    parse_used_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResumeParseCache(Base):
    """
    The LLM extraction for (resume text, prompt, model, detection version) -
    a repeat parse of the same input returns the same JSON instead of a new,
    possibly different, answer. Keyed by the model that actually answered,
    so a backup model's answer is never served as the main model's. Re-parse
    always bypasses it.
    """

    __tablename__ = "resume_parse_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    detection_version: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_extraction: Mapped[dict] = mapped_column(JSONB, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_hit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CoreSkillConfirmation(Base):
    """
    LLM yes/no + quote check of a resume's evidence for a JD's core skills.
    Cached by (resume text, JD core skill set incl. senses, prompt version,
    detection version, model): the same question is never asked twice.
    results: [{"canonical_skill_id", "sense", "confirmed", "quote",
               "quote_verified", "reason"}].
    """

    __tablename__ = "core_skill_confirmations"
    __table_args__ = (Index("ix_core_skill_confirmations_resume_jd", "resume_id", "jd_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    jd_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    core_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_version: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    used_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CandidateScoringResult(Base):
    """
    One deterministic scoring of a campaign candidate by one scoring version
    (v1 / v2), with the exact config it used - so v1 and v2 can be compared
    (shadow report) and any score can be reproduced. acted_on marks the
    version whose outcome drove the pipeline.
    """

    __tablename__ = "candidate_scoring_results"
    __table_args__ = (
        Index("ix_candidate_scoring_results_cc_version", "campaign_candidate_id", "scoring_version", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False,
    )
    scoring_version: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    scoring_config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    acted_on: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResumeParseVersion(Base):
    """
    A resume's previous parse, kept when it is re-parsed - with what
    changed (skills added/removed, core count before/after per campaign).
    """

    __tablename__ = "resume_parse_versions"
    __table_args__ = (UniqueConstraint("resume_id", "version_no", name="uq_resume_parse_versions_resume_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parsed_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    skills_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    diff: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillPatternRule(Base):
    """
    A wording pattern that names a catalogue skill without its exact name -
    e.g. "AI" / "responsible AI" within 3 words of "governance" in an
    Experience section -> AI Governance. Matches are DERIVED evidence: never
    auto-confirmed, always sent to the core confirmation vote.
    """

    __tablename__ = "skill_pattern_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    anchor_terms = mapped_column(ARRAY(Text), nullable=False)
    near_terms = mapped_column(ARRAY(Text), nullable=False)
    # Words allowed between an anchor term and a near term (either order).
    max_gap_words: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    # resume_sections names (EXPERIENCE, SUMMARY, ...) the rule applies in.
    sections = mapped_column(ARRAY(String(30)), nullable=False)
    # Anchor terms in their written casing only ("AI", never "ai"/"Ai").
    anchor_case_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # LINE: resume text lines in `sections`; JOB_TITLE: the parsed
    # work-experience titles (sections ignored).
    match_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="LINE", server_default=text("'LINE'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScoringShadowRanking(Base):
    """
    A recruiter's own ranking of a campaign's candidates (e.g. the hiring
    lead's order), entered in the shadow report to compare with v1 and v2.
    One row per campaign candidate; never used for any decision.
    """

    __tablename__ = "scoring_shadow_rankings"
    __table_args__ = (Index("ix_scoring_shadow_rankings_campaign_id", "campaign_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False, unique=True,
    )
    recruiter_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Whose ranking it is (e.g. "Rakesh K") - may differ from who typed it in.
    ranked_by_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entered_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
