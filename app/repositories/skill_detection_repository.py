"""Scoring v2 skill detection storage: scan rules, senses, resume evidence, parse cache, core confirmations."""
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.scoring_v2 import (
    Certification, CoreSkillConfirmation, ResumeParseCache, ResumeProvisionalSkillScan, ResumeSkillEvidence,
    SkillPatternRule, SkillScanTerm, SkillSense,
)
from app.services.skills.resume_skill_scanner import PatternRule, ScanTermRule, term_key
from app.services.skills.skill_sense_resolver import SenseDef

logger = logging.getLogger(__name__)



class SkillDetectionRepository:
    def __init__(self, db: Session):
        self.db = db

    # --- catalogue data -------------------------------------------------
    def load_scan_rules(self) -> dict[str, ScanTermRule]:
        rows = self.db.execute(select(SkillScanTerm)).scalars().all()
        return {
            term_key(row.term_key): ScanTermRule(row.policy, tuple(row.cue_words or ()))
            for row in rows
        }

    def load_senses(self) -> list[SenseDef]:
        rows = self.db.execute(select(SkillSense).where(SkillSense.is_active.is_(True))).scalars().all()
        return [
            SenseDef(
                skill_id=row.canonical_skill_id, sense_key=row.sense_key, label=row.label,
                description=row.description, cue_words=tuple(row.cue_words or ()),
                is_default=row.is_default, window_lines=row.window_lines,
            )
            for row in rows
        ]

    def load_pattern_rules(self) -> list[PatternRule]:
        rows = self.db.execute(select(SkillPatternRule).where(SkillPatternRule.is_active.is_(True))).scalars().all()
        return [
            PatternRule(
                skill_id=row.canonical_skill_id, name=row.name, anchor_terms=tuple(row.anchor_terms or ()),
                near_terms=tuple(row.near_terms or ()), max_gap_words=row.max_gap_words,
                sections=frozenset(row.sections or ()), anchor_case_sensitive=row.anchor_case_sensitive,
                match_scope=row.match_scope or "LINE",
            )
            for row in rows
        ]

    def load_certification_rules(self) -> list:
        """Mapped certifications (with their skills) for build_evidence - Phase 3 Step 5."""
        from app.services.skills.certification_mapping_service import rule_from
        certifications = self.db.execute(select(Certification)).scalars().all()
        return [rule_from(c.name, list(c.name_variants or []), [(s.canonical_skill_id, s.sense_key) for s in c.skills])
                for c in certifications if c.skills]

    def load_evidence_rules_config(self) -> dict | None:
        """
        The current scoring version's "evidence" (qualifiers, work verbs) - None = the defaults. Detection
        is shared by every scoring version (one set of evidence rows per resume), so only the current
        version's rules apply; a candidate version can't change them (scoring_versions).
        """
        from app.services.campaign.scoring_versions import current_version
        version = current_version(self.db)
        configuration = version.configuration if version is not None else None
        evidence = configuration.get("evidence") if isinstance(configuration, dict) else None
        return evidence if isinstance(evidence, dict) else None

    def load_match_blocklist(self) -> list[dict]:
        """The current scoring version's match_blocklist - a list of {"term", "skill", "note"} (settings
        phase 3; platform_config SCORING_V2_MATCH_BLOCKLIST before)."""
        from app.services.campaign.scoring_v2 import merge_config
        from app.services.campaign.scoring_versions import current_version, version_config
        version = current_version(self.db)
        entries = (version_config(version) if version is not None else merge_config(None)).get("match_blocklist")
        return entries if isinstance(entries, list) else []

    # --- evidence -------------------------------------------------------
    def replace_evidence(self, resume_id: UUID, rows: list[ResumeSkillEvidence]) -> None:
        """Replaces the resume's evidence as a whole, inside a savepoint (a failure never aborts the caller's transaction)."""
        with self.db.begin_nested():
            self.db.execute(delete(ResumeSkillEvidence).where(ResumeSkillEvidence.resume_id == resume_id))
            self.db.add_all(rows)

    def list_evidence(self, resume_id: UUID) -> list[ResumeSkillEvidence]:
        return list(self.db.execute(
            select(ResumeSkillEvidence).where(ResumeSkillEvidence.resume_id == resume_id)
        ).scalars().all())

    # --- parse cache ----------------------------------------------------
    def get_parse_cache(self, cache_key: str) -> ResumeParseCache | None:
        return self.db.execute(
            select(ResumeParseCache).where(ResumeParseCache.cache_key == cache_key)
        ).scalar_one_or_none()

    def mark_parse_cache_hit(self, entry: ResumeParseCache) -> None:
        with self.db.begin_nested():
            entry.hit_count = (entry.hit_count or 0) + 1
            entry.last_hit_at = datetime.now(timezone.utc)

    def resume_text_hashes(self, resume_ids: list[UUID]) -> set[str]:
        """
        Candidate erasure - the hashes of these resumes' redacted text: the ones
        each parse recorded on the resume (resumes.parse_text_hashes), and the
        ones the rows derived from it recorded (skill evidence, provisional
        scans, core-skill confirmations). The parse cache is keyed by the same hash.
        """
        if not resume_ids:
            return set()
        from app.models.candidates import Resume
        hashes: set[str] = set()
        for stored in self.db.execute(select(Resume.parse_text_hashes).where(Resume.id.in_(resume_ids))).scalars():
            hashes |= set(stored or [])
        for model in (ResumeSkillEvidence, ResumeProvisionalSkillScan, CoreSkillConfirmation):
            hashes |= set(self.db.execute(
                select(model.text_hash).where(model.resume_id.in_(resume_ids), model.text_hash.is_not(None)).distinct()
            ).scalars())
        hashes.discard("")
        return hashes

    def delete_parse_cache_by_text_hashes(self, text_hashes) -> int:
        """Candidate erasure - the cached LLM extraction of their resume text (any prompt/model)."""
        text_hashes = [h for h in text_hashes if h]
        if not text_hashes:
            return 0
        deleted = self.db.execute(delete(ResumeParseCache).where(ResumeParseCache.text_hash.in_(text_hashes))).rowcount
        self.db.flush()
        return deleted or 0

    def put_parse_cache(self, **values) -> None:
        """Insert or replace (a re-parse overwrites the earlier answer for the same key)."""
        with self.db.begin_nested():
            existing = self.get_parse_cache(values["cache_key"])
            if existing is None:
                self.db.add(ResumeParseCache(**values))
            else:
                for name, value in values.items():
                    setattr(existing, name, value)
                existing.hit_count = 0
                existing.last_hit_at = None

    # --- core confirmations ---------------------------------------------
    def get_confirmation(self, cache_key: str) -> CoreSkillConfirmation | None:
        return self.db.execute(
            select(CoreSkillConfirmation).where(CoreSkillConfirmation.cache_key == cache_key)
        ).scalar_one_or_none()

    def put_confirmation(self, row: CoreSkillConfirmation) -> None:
        """Insert, or merge the new final answers into the cached ones for the same key."""
        with self.db.begin_nested():
            existing = self.get_confirmation(row.cache_key)
            if existing is None:
                self.db.add(row)
                return
            merged = {r["canonical_skill_id"]: r for r in existing.results or []}
            merged.update({r["canonical_skill_id"]: r for r in row.results or []})
            existing.results = list(merged.values())
