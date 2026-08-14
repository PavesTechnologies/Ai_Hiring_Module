from enum import Enum
from uuid import UUID

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository

# Max child traversal depth for the hierarchy fallback: 1 = direct CHILD,
# 2 = GRANDCHILD. Direct child always takes precedence over grandchild.
_MAX_HIERARCHY_DEPTH = 2

_DEFAULT_GRANDCHILD_MULTIPLIER = 0.50

# Skill-importance multipliers (core/supporting) - default to neutral 1.0
# (no differentiation) so a JD with no importance-classified skills, or a
# platform_config that hasn't set these keys yet, scores byte-for-byte the
# same as before this feature existed. Never hardcode a differentiated
# business value here - only platform_config may do that.
_DEFAULT_CORE_IMPORTANCE_MULTIPLIER = 1.0
_DEFAULT_SUPPORTING_IMPORTANCE_MULTIPLIER = 1.0

# Business rule (not a percentage, not proportional to required-skill
# count): a candidate may be missing at most this many CORE required
# skills, regardless of whether the JD has 10 or 100 required skills.
DEFAULT_MAX_MISSING_CORE_SKILLS = 3

# No prior concept of a coverage gate exists - 0.0 (always satisfied)
# keeps every caller that doesn't pass this explicitly behaving exactly as
# it did before this feature existed.
DEFAULT_REQUIRED_SKILL_COVERAGE_THRESHOLD = 0.0

# M07-E03 S01 T02: human-readable display names for
# ExperienceEducationValidationService's internal degree-level names -
# rejection_reason must never surface an internal level code verbatim.
# Matches app.enums.education.DEGREE_LEVEL_RANK's keys (the only levels
# ExperienceEducationValidationService can ever produce, now that it
# sources degree_level from DegreeLevel/DEGREE_LEVEL_RANK - PROFESSIONAL/
# OTHER/UNKNOWN are deliberately unranked there and never reach here).
_DEGREE_LEVEL_DISPLAY_NAMES = {
    "CERTIFICATE": "Certificate",
    "DIPLOMA": "Diploma",
    "ASSOCIATE": "Associate's",
    "BACHELOR": "Bachelor's",
    "POSTGRADUATE_DIPLOMA": "Postgraduate Diploma",
    "MASTER": "Master's",
    "DOCTORATE": "Doctorate",
}


def _degree_level_display(level_name: str | None) -> str:
    if level_name is None:
        return "no qualifying degree"
    return _DEGREE_LEVEL_DISPLAY_NAMES.get(level_name, level_name.replace("_", " ").title())


def _format_years(value: float) -> str:
    """4.0 -> '4', 2.5 -> '2.5' - matches the ticket's example wording."""
    rounded = round(value, 1)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


class MandatorySkillMatchType(str, Enum):
    """Match-type vocabulary for the mandatory-skill coverage breakdown (M07-E01 S02 T02)."""

    EXACT = "EXACT"
    CHILD = "CHILD"
    GRANDCHILD = "GRANDCHILD"
    SIBLING = "SIBLING"
    SEMANTIC = "SEMANTIC"
    MISSING = "MISSING"


# Tiers that count as "covered" for mandatory_coverage_pct (T03) - only
# MISSING does not count.
_COVERED_MATCH_TYPE_VALUES = {
    MandatorySkillMatchType.EXACT.value,
    MandatorySkillMatchType.CHILD.value,
    MandatorySkillMatchType.GRANDCHILD.value,
    MandatorySkillMatchType.SIBLING.value,
    MandatorySkillMatchType.SEMANTIC.value,
}


class CandidateScoringService:

    def __init__(
        self,
        skill_repository: SkillRepository,
        skill_ontology_repository: SkillOntologyRepository | None = None,
        config_repository: ConfigRepository | None = None,
        campaign_candidate_repository: CampaignCandidateRepository | None = None,
    ):
        self.skill_repository = skill_repository
        self.skill_ontology_repository = skill_ontology_repository
        self.config_repository = config_repository
        self.campaign_candidate_repository = campaign_candidate_repository

    def calculate_deterministic_score(
        self,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
    ) -> tuple[float, bool]:
        """
        Deterministic score based on mandatory JD skills matched against
        candidate normalized skills - delegates entirely to
        build_mandatory_skill_breakdown so this always reflects the same
        weighted SUM(skill_contribution)/SUM(configured_weight) x 100
        formula, never a count/ratio of how many skills matched. A
        count-based formula must never be reintroduced here even as a
        "simpler" no-hierarchy alternative - it would silently diverge from
        the one true deterministic_score calculation and duplicate logic
        that already exists in build_mandatory_skill_breakdown.

        Requires skill_ontology_repository/config_repository for the same
        reason build_mandatory_skill_breakdown does - see its docstring.
        """
        breakdown = self.build_mandatory_skill_breakdown(jd_id, resume_id)

        any_missing = any(
            skill["match_type"] == MandatorySkillMatchType.MISSING.value
            for skill in breakdown["mandatory_skills"]
        )
        passed = not any_missing and breakdown["deterministic_score"] >= float(deterministic_threshold)

        return breakdown["deterministic_score"], passed

    # ------------------------------------------------------------------
    # M07-E01 S02: hierarchy-aware mandatory-skill coverage breakdown
    # ------------------------------------------------------------------

    def load_mandatory_skill_hierarchy_for_jd(self, jd_id: UUID) -> dict:
        """
        Convenience wrapper for callers that only have a jd_id (not
        already-fetched coverage_rows) - e.g. ResumeSelectionService
        comparing several resume versions against the same JD, where the
        hierarchy should be loaded once up front rather than once per
        build_mandatory_skill_breakdown call.
        """
        mandatory_jd_skills = self.skill_repository.get_mandatory_jd_skills(jd_id)
        return self.load_mandatory_skill_hierarchy(
            [jd_skill.canonical_skill_id for jd_skill in mandatory_jd_skills]
        )

    def load_mandatory_skill_hierarchy(self, mandatory_skill_ids: list[UUID]) -> dict:
        """
        Batch-loads everything the CHILD/GRANDCHILD/SIBLING hierarchy tiers
        need for a fixed set of mandatory skill ids, in 3 queries total
        (skills-themselves, their children/siblings, their grandchildren)
        instead of one get_skill_by_id + one-or-more get_children call per
        skill. This data depends only on the JD's mandatory skills, never
        on which resume/candidate is being scored - callers comparing
        multiple resume versions against the same JD (e.g.
        ResumeSelectionService._compare_and_select) should call this once
        and pass the result to every build_mandatory_skill_breakdown call
        for that JD, instead of letting each one reload it.
        """
        skills_by_id = self.skill_ontology_repository.get_skills_by_ids(mandatory_skill_ids)

        parent_ids = {
            skill.parent_skill_id for skill in skills_by_id.values() if skill.parent_skill_id is not None
        }
        children_by_parent = self.skill_ontology_repository.get_children_batch(
            list(set(mandatory_skill_ids) | parent_ids)
        )

        child_ids = {
            child.id
            for skill_id in mandatory_skill_ids
            for child in children_by_parent.get(skill_id, [])
            if child.is_active
        }
        grandchildren_by_parent = self.skill_ontology_repository.get_children_batch(list(child_ids))

        return {
            "skills_by_id": skills_by_id,
            "children_by_parent": children_by_parent,
            "grandchildren_by_parent": grandchildren_by_parent,
        }

    def build_mandatory_skill_breakdown(
        self,
        jd_id: UUID,
        resume_id: UUID,
        hierarchy: dict | None = None,
    ) -> dict:
        """
        Per-mandatory-skill match breakdown in strict priority order:
        EXACT -> CHILD -> GRANDCHILD -> SIBLING -> SEMANTIC -> MISSING.
        Every mandatory JD skill gets exactly one entry, and a skill is
        never scored against more than one tier (first match wins).

        Requires skill_ontology_repository (hierarchy traversal) and
        config_repository (HIERARCHY_GRANDCHILD_MULTIPLIER /
        HIERARCHY_SEMANTIC_ONLY_THRESHOLD) - calculate_deterministic_score
        delegates here, so it requires both too.

        `hierarchy` is the JD-only batch-loaded bundle from
        load_mandatory_skill_hierarchy - pass it in when scoring multiple
        resumes against the same JD (it never changes between them) to
        avoid recomputing it per resume. Left None (the default) for the
        common single-resume case, where it's loaded internally.
        """
        if self.skill_ontology_repository is None or self.config_repository is None:
            raise ValueError(
                "CandidateScoringService requires skill_ontology_repository and "
                "config_repository for hierarchy-aware mandatory-skill scoring."
            )

        coverage_rows = self.skill_repository.get_mandatory_skill_coverage(jd_id, resume_id)

        if hierarchy is None:
            hierarchy = self.load_mandatory_skill_hierarchy(
                [row.canonical_skill_id for row in coverage_rows]
            )

        # Candidate's own in-play normalized skills (scoring_weight > 0),
        # keyed by canonical_skill_id - the pool every hierarchy tier below
        # matches against. The resume/canonical unique index means at most
        # one row per canonical_skill_id.
        candidate_skills_by_id = {
            skill.canonical_skill_id: skill
            for skill in self.skill_repository.get_candidate_normalized_skills(resume_id)
            if skill.canonical_skill_id is not None and skill.scoring_weight and skill.scoring_weight > 0
        }

        # S04-T01: zero verified (scoring_weight > 0) candidate skills is a
        # distinct condition from a resume parse failure - parsing already
        # succeeded (the caller only gets here once parse_status == PARSED),
        # but nothing extracted from it normalized to a usable skill. Every
        # mandatory skill is forced to MISSING below without even attempting
        # hierarchy traversal, since an empty candidate pool guarantees no
        # tier could ever match.
        no_verified_skills = len(candidate_skills_by_id) == 0

        configs = self.config_repository.get_configs_by_keys(
            [
                "HIERARCHY_GRANDCHILD_MULTIPLIER", "HIERARCHY_SEMANTIC_ONLY_THRESHOLD",
                "CORE_IMPORTANCE_WEIGHT_MULTIPLIER", "SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER",
            ]
        )
        grandchild_multiplier = float(configs.get("HIERARCHY_GRANDCHILD_MULTIPLIER", _DEFAULT_GRANDCHILD_MULTIPLIER))
        # No safe default exists for this one (unlike grandchild's ticket-
        # specified "0.5 or platform_config value") - if it's not
        # configured, the SEMANTIC tier is skipped entirely rather than
        # inventing a threshold, and every otherwise-semantic skill simply
        # falls through to MISSING.
        semantic_threshold_raw = configs.get("HIERARCHY_SEMANTIC_ONLY_THRESHOLD")
        semantic_threshold = float(semantic_threshold_raw) if semantic_threshold_raw is not None else None

        core_multiplier = float(
            configs.get("CORE_IMPORTANCE_WEIGHT_MULTIPLIER", _DEFAULT_CORE_IMPORTANCE_MULTIPLIER)
        )
        supporting_multiplier = float(
            configs.get("SUPPORTING_IMPORTANCE_WEIGHT_MULTIPLIER", _DEFAULT_SUPPORTING_IMPORTANCE_MULTIPLIER)
        )

        mandatory_skills = []
        matched_count = 0

        for row in coverage_rows:
            entry = self._score_one_mandatory_skill(
                row, candidate_skills_by_id, grandchild_multiplier, semantic_threshold,
                core_multiplier, supporting_multiplier, hierarchy,
            )
            if entry["match_type"] in _COVERED_MATCH_TYPE_VALUES:
                matched_count += 1
            mandatory_skills.append(entry)

        total_mandatory = len(mandatory_skills)
        # mandatory_coverage_pct: display-only, a pure count-based coverage
        # metric ("how many mandatory skills matched at all, regardless of
        # tier quality"). Never used as the deterministic score - but IS
        # reused as-is (never recalculated a second way) for the Part 5
        # required-skill-coverage qualification check - see
        # evaluate_skill_qualification.
        mandatory_coverage_pct = (
            round((matched_count / total_mandatory) * 100, 2) if total_mandatory > 0 else 100.0
        )

        # deterministic_score = (SUM mandatory contributions / SUM max
        # mandatory contributions) x 100. The "max" contribution for a
        # skill is what it would have contributed on a perfect EXACT match
        # (hierarchy_multiplier=1.0, candidate_scoring_weight=1.0) - i.e.
        # effective_jd_weight itself (jd_skill.weight x its
        # importance_multiplier - jd_skill.weight alone when the skill
        # isn't core/supporting-classified, since that multiplier defaults
        # to 1.0). This ratio is what makes the score a true 0-100 scale
        # regardless of the actual magnitude JD skill weights happen to use
        # (equal-weight auto-assignment or a future manual override) - no
        # fixed point budget is assumed or required.
        actual_sum = sum(entry["skill_contribution"] or 0 for entry in mandatory_skills)
        max_sum = sum(entry["effective_jd_weight"] or 0 for entry in mandatory_skills)
        deterministic_score = (
            round((actual_sum / max_sum) * 100, 2) if max_sum > 0 else 100.0
        )

        return {
            "mandatory_skills": mandatory_skills,
            "mandatory_coverage_pct": mandatory_coverage_pct,
            "deterministic_score": deterministic_score,
            "semantic_tier_available": semantic_threshold is not None,
            "NO_VERIFIED_SKILLS": no_verified_skills,
        }

    def _score_one_mandatory_skill(
        self,
        row,
        candidate_skills_by_id: dict,
        grandchild_multiplier: float,
        semantic_threshold: float | None,
        core_multiplier: float = _DEFAULT_CORE_IMPORTANCE_MULTIPLIER,
        supporting_multiplier: float = _DEFAULT_SUPPORTING_IMPORTANCE_MULTIPLIER,
        hierarchy: dict | None = None,
    ) -> dict:
        canonical_skill_id = row.canonical_skill_id
        weight = float(row.weight) if row.weight is not None else None
        mandatory = bool(row.mandatory)

        # getattr, not row.importance: some callers (older fixtures, any
        # future row-like object) may not carry this attribute at all -
        # absence must mean "not classified", identical to an explicit NULL.
        importance_label = self._importance_label(getattr(row, "importance", None))
        importance_multiplier = self._importance_multiplier_for_label(
            importance_label, core_multiplier, supporting_multiplier,
        )

        # hierarchy is batch-loaded once for every mandatory skill on this
        # JD (see load_mandatory_skill_hierarchy) - looked up here instead
        # of a live get_skill_by_id/get_children call per skill.
        skills_by_id = hierarchy["skills_by_id"]
        children_by_parent = hierarchy["children_by_parent"]
        grandchildren_by_parent = hierarchy["grandchildren_by_parent"]

        # Fetched once up front (not just for the SIBLING tier as before) -
        # every entry needs the JD skill's own canonical_name (T03), and
        # SIBLING/SEMANTIC still need target_skill itself below.
        target_skill = skills_by_id.get(canonical_skill_id)
        canonical_name = target_skill.canonical_name if target_skill is not None else None

        # Tier 1: EXACT - already resolved by the T01 LEFT JOIN. The
        # matched candidate skill IS the JD skill itself here.
        if row.candidate_scoring_weight is not None:
            return self._breakdown_entry(
                canonical_skill_id, canonical_name, mandatory, weight,
                MandatorySkillMatchType.EXACT, 1.0,
                float(row.candidate_scoring_weight), row.match_tier, row.confidence,
                matched_candidate_skill_canonical_name=canonical_name,
                importance=importance_label, importance_multiplier=importance_multiplier,
            )

        # Tier 2: CHILD (depth 1). Inactive (deactivated/deprecated) skills
        # are never valid hierarchy match targets (S03-T01), even if a
        # stale candidate_skills row still points at one.
        children = [
            child for child in children_by_parent.get(canonical_skill_id, [])
            if child.is_active
        ]
        child_match = self._best_hierarchy_match(children, candidate_skills_by_id)
        if child_match is not None:
            matched_ontology_skill, candidate_skill = child_match
            return self._breakdown_entry(
                canonical_skill_id, canonical_name, mandatory, weight,
                MandatorySkillMatchType.CHILD, 0.7,
                float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                matched_candidate_skill_canonical_name=matched_ontology_skill.canonical_name,
                importance=importance_label, importance_multiplier=importance_multiplier,
            )

        # Tier 3: GRANDCHILD (depth 2) - only reached because no direct
        # child matched above, so direct child always takes precedence.
        if _MAX_HIERARCHY_DEPTH >= 2:
            grandchildren = [
                grandchild
                for child in children
                for grandchild in grandchildren_by_parent.get(child.id, [])
                if grandchild.is_active
            ]
            grandchild_match = self._best_hierarchy_match(grandchildren, candidate_skills_by_id)
            if grandchild_match is not None:
                matched_ontology_skill, candidate_skill = grandchild_match
                return self._breakdown_entry(
                    canonical_skill_id, canonical_name, mandatory, weight,
                    MandatorySkillMatchType.GRANDCHILD, grandchild_multiplier,
                    float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                    matched_candidate_skill_canonical_name=matched_ontology_skill.canonical_name,
                    importance=importance_label, importance_multiplier=importance_multiplier,
                )

        # Tier 4: SIBLING - only if no exact/child/grandchild match.
        sibling_skip_reason = None

        if target_skill is None:
            sibling_skip_reason = "SIBLING_SKIPPED"
        elif target_skill.parent_skill_id is None:
            sibling_skip_reason = "ROOT_SKILL_NO_PARENT"
        else:
            siblings = [
                sibling
                for sibling in children_by_parent.get(target_skill.parent_skill_id, [])
                if sibling.id != canonical_skill_id and sibling.is_active
            ]
            sibling_match = self._best_hierarchy_match(siblings, candidate_skills_by_id)
            if sibling_match is not None:
                matched_ontology_skill, candidate_skill = sibling_match
                return self._breakdown_entry(
                    canonical_skill_id, canonical_name, mandatory, weight,
                    MandatorySkillMatchType.SIBLING, 0.4,
                    float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                    matched_candidate_skill_canonical_name=matched_ontology_skill.canonical_name,
                    importance=importance_label, importance_multiplier=importance_multiplier,
                )

        # Tier 5: SEMANTIC - only if every higher tier failed, and only if
        # both a threshold is configured and the mandatory skill itself has
        # an embedding to compare against.
        if semantic_threshold is not None and target_skill is not None and target_skill.embedding is not None:
            semantic_result = self.skill_repository.find_best_semantic_match(
                target_skill.embedding, list(candidate_skills_by_id.keys())
            )
            if semantic_result is not None:
                matched_skill_id, similarity = semantic_result
                if similarity >= semantic_threshold:
                    candidate_skill = candidate_skills_by_id[matched_skill_id]
                    matched_ontology_skill = self.skill_ontology_repository.get_skill_by_id(matched_skill_id)
                    entry = self._breakdown_entry(
                        canonical_skill_id, canonical_name, mandatory, weight,
                        MandatorySkillMatchType.SEMANTIC, 0.2,
                        float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                        matched_candidate_skill_canonical_name=(
                            matched_ontology_skill.canonical_name if matched_ontology_skill is not None else None
                        ),
                        importance=importance_label, importance_multiplier=importance_multiplier,
                    )
                    entry["semantic_similarity"] = round(similarity, 4)
                    return entry

        # MISSING - nothing matched at any tier.
        entry = self._breakdown_entry(
            canonical_skill_id, canonical_name, mandatory, weight,
            MandatorySkillMatchType.MISSING, 0.0, None, None, None,
            matched_candidate_skill_canonical_name=None,
            importance=importance_label, importance_multiplier=importance_multiplier,
        )
        if sibling_skip_reason is not None:
            entry["sibling_skip_reason"] = sibling_skip_reason
        return entry

    @staticmethod
    def _importance_label(raw_importance) -> str | None:
        """
        Normalizes a JDSkill.importance value (a JDSkillImportance enum
        member, a plain "CORE"/"SUPPORTING" string, or None/missing) to a
        lowercase "core"/"supporting" label, or None when not classified -
        legacy JDSkill rows, preferred skills, or rows from a caller that
        never queried this column at all.
        """
        if raw_importance is None:
            return None
        value = raw_importance.value if hasattr(raw_importance, "value") else str(raw_importance)
        return value.lower()

    @staticmethod
    def _importance_multiplier_for_label(
        importance_label: str | None, core_multiplier: float, supporting_multiplier: float,
    ) -> float:
        if importance_label == "core":
            return core_multiplier
        if importance_label == "supporting":
            return supporting_multiplier
        # Not classified - Part 21: NULL must be a neutral 1.0 multiplier,
        # never guessed as either tier.
        return 1.0

    @staticmethod
    def _best_hierarchy_match(ontology_skills: list, candidate_skills_by_id: dict):
        """
        Among ontology_skills (children/grandchildren/siblings of the
        mandatory skill), returns a (matched_ontology_skill, CandidateSkill)
        pair for the candidate's matching skill with the highest
        scoring_weight, or None if the candidate has none of them.
        """
        matches = [
            (skill, candidate_skills_by_id[skill.id])
            for skill in ontology_skills
            if skill.id in candidate_skills_by_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda pair: float(pair[1].scoring_weight))

    @staticmethod
    def _breakdown_entry(
        canonical_skill_id: UUID,
        canonical_name: str | None,
        mandatory: bool,
        weight: float | None,
        match_type: MandatorySkillMatchType,
        hierarchy_score_multiplier: float,
        candidate_scoring_weight: float | None,
        match_tier: str | None,
        confidence: float | None,
        matched_candidate_skill_canonical_name: str | None = None,
        importance: str | None = None,
        importance_multiplier: float = 1.0,
    ) -> dict:
        # effective_jd_weight = jd_skill.weight x importance_multiplier
        # (Part 3) - jd_skills.weight itself is never mutated; this is a
        # second, derived number computed fresh every time, same as
        # hierarchy_score_multiplier already is. Computed unconditionally
        # (including for MISSING) because build_mandatory_skill_breakdown's
        # SUM(effective_jd_weight) denominator needs every mandatory
        # skill's max-possible contribution, matched or not - exactly how
        # configured_weight was already used in that denominator before.
        effective_jd_weight = round(weight * importance_multiplier, 4) if weight is not None else None

        if match_type == MandatorySkillMatchType.MISSING:
            # Requirement: a final unmatched skill always contributes 0,
            # even when weight itself is unset.
            contribution = 0.0
        elif effective_jd_weight is None or candidate_scoring_weight is None:
            # Can't compute effective_jd_weight * candidate_skill.scoring_weight
            # * hierarchy_score_multiplier without both factors - reported
            # as unknown rather than silently defaulted.
            contribution = None
        else:
            contribution = round(effective_jd_weight * candidate_scoring_weight * hierarchy_score_multiplier, 4)

        return {
            "canonical_skill_id": str(canonical_skill_id),
            "canonical_name": canonical_name,
            "mandatory": mandatory,
            "configured_weight": weight,
            "importance": importance,
            "importance_multiplier": importance_multiplier,
            "effective_jd_weight": effective_jd_weight,
            "match_type": match_type.value,
            "matched_candidate_skill_canonical_name": matched_candidate_skill_canonical_name,
            "hierarchy_score_multiplier": hierarchy_score_multiplier,
            "candidate_scoring_weight": candidate_scoring_weight,
            "match_tier": match_tier,
            "confidence": confidence,
            "skill_contribution": contribution,
        }

    # ------------------------------------------------------------------
    # Preferred (non-mandatory) skills: recorded for Composite Score use
    # only - per M07, preferred skills must NEVER contribute to
    # deterministic_score.
    # ------------------------------------------------------------------

    def build_preferred_skill_breakdown(
        self,
        jd_id: UUID,
        resume_id: UUID,
    ) -> dict:
        """
        Preferred JD skills (jd_skills.mandatory = FALSE), EXACT
        canonical-match only - unlike mandatory skills, no CHILD/
        GRANDCHILD/SIBLING/SEMANTIC fallback applies to preferred skills;
        an unmatched preferred skill simply contributes 0, never MISSING
        in the mandatory-coverage sense (it never affects
        mandatory_coverage_pct, deterministic_score, or deterministic_passed
        - preferred_skill_bonus is stored in score_breakdown purely for a
        future Composite Score calculation to consume).

        Reuses SkillRepository.get_mandatory_skill_coverage(mandatory=False)
        - the same LEFT JOIN T01 already established for mandatory
        coverage - and _breakdown_entry, so the per-skill JSON shape is
        identical to a mandatory_skills entry.

        contribution = jd_skill.weight * candidate_skill.scoring_weight * 1.0
        """
        coverage_rows = self.skill_repository.get_mandatory_skill_coverage(jd_id, resume_id, mandatory=False)
        skills_by_id = self.skill_ontology_repository.get_skills_by_ids(
            [row.canonical_skill_id for row in coverage_rows]
        )

        preferred_skills = []
        for row in coverage_rows:
            weight = float(row.weight) if row.weight is not None else None
            is_exact_match = row.candidate_scoring_weight is not None
            target_skill = skills_by_id.get(row.canonical_skill_id)
            canonical_name = target_skill.canonical_name if target_skill is not None else None
            preferred_skills.append(self._breakdown_entry(
                row.canonical_skill_id, canonical_name, bool(row.mandatory), weight,
                MandatorySkillMatchType.EXACT if is_exact_match else MandatorySkillMatchType.MISSING,
                1.0 if is_exact_match else 0.0,
                float(row.candidate_scoring_weight) if is_exact_match else None,
                row.match_tier, row.confidence,
                matched_candidate_skill_canonical_name=canonical_name if is_exact_match else None,
            ))

        preferred_skill_bonus = round(sum(entry["skill_contribution"] or 0 for entry in preferred_skills), 4)

        return {
            "preferred_skills": preferred_skills,
            "preferred_skill_bonus": preferred_skill_bonus,
        }

    # ------------------------------------------------------------------
    # Skill-stage qualification (core/supporting importance): three checks
    # computed purely from the breakdown build_mandatory_skill_breakdown
    # already produced - never a second, independent evaluation, never a
    # second scoring path. Replaces the old "any missing mandatory skill
    # -> automatic fail" gate.
    # ------------------------------------------------------------------

    def evaluate_skill_qualification(
        self,
        breakdown: dict,
        skill_score: float,
        deterministic_threshold: float,
        required_skill_coverage_threshold: float = DEFAULT_REQUIRED_SKILL_COVERAGE_THRESHOLD,
        max_missing_core_skills: int = DEFAULT_MAX_MISSING_CORE_SKILLS,
    ) -> dict:
        """
        The skill stage passes only when ALL three hold:

        1. required_skill_coverage (breakdown["mandatory_coverage_pct"],
           reused as-is, never recalculated) >= required_skill_coverage_threshold
        2. skill_score (the importance-weighted deterministic skill score)
           >= deterministic_threshold - the SAME campaign.deterministic_threshold
           every other deterministic comparison already uses, not a second
           threshold.
        3. missing CORE required skills <= max_missing_core_skills (a fixed
           business limit, default 3 - never proportional to how many
           required skills the JD has).

        A required skill whose importance was never classified (NULL - a
        legacy JDSkill row) never counts toward #3, but still counts
        against #1 and #2 exactly as any other missing mandatory skill
        would - it simply isn't eligible for the CORE-specific limit.
        """
        coverage_pct = breakdown["mandatory_coverage_pct"]
        coverage_passed = coverage_pct >= required_skill_coverage_threshold

        missing_core_skill_count = sum(
            1 for skill in breakdown["mandatory_skills"]
            if skill["match_type"] == MandatorySkillMatchType.MISSING.value and skill.get("importance") == "core"
        )
        core_gap_passed = missing_core_skill_count <= max_missing_core_skills

        score_passed = skill_score >= float(deterministic_threshold)

        return {
            "required_skill_coverage_threshold": float(required_skill_coverage_threshold),
            "coverage_passed": coverage_passed,
            "score_passed": score_passed,
            "missing_core_skill_count": missing_core_skill_count,
            "max_missing_core_skills": max_missing_core_skills,
            "core_gap_passed": core_gap_passed,
            "skill_qualification_passed": coverage_passed and score_passed and core_gap_passed,
        }

    # ------------------------------------------------------------------
    # M07-E03 S01 T02: human-readable rejection_reason summary
    # ------------------------------------------------------------------

    MAX_REJECTION_REASON_LENGTH = 500

    @staticmethod
    def build_rejection_reason(
        breakdown: dict,
        experience_result: dict | None = None,
        education_result: dict | None = None,
    ) -> str:
        """
        Builds candidate_rejections.rejection_reason from the same
        breakdown/experience_result/education_result already computed for
        scoring - never a second, independent evaluation. Concatenates
        every applicable failure with " | " so a candidate rejected for
        multiple reasons at once (e.g. missing skills AND insufficient
        experience) reports all of them, not just the first one found.

        Only canonical_name / plain float / human-readable degree-level
        text is ever interpolated - no canonical_skill_id, no match_type
        code, no database column name.
        """
        clauses: list[str] = []

        if breakdown.get("NO_VERIFIED_SKILLS"):
            clauses.append("No verifiable skills extracted from resume")
        else:
            missing_skill_names = [
                skill["canonical_name"] or skill["canonical_skill_id"]
                for skill in breakdown.get("mandatory_skills", [])
                if skill["match_type"] == MandatorySkillMatchType.MISSING.value
            ]
            if missing_skill_names:
                clauses.append(f"Missing required skills: {', '.join(missing_skill_names)}")

        # Explainability for the three skill-qualification checks (Part
        # 5-7) - only added when the corresponding check actually failed,
        # since a candidate can now have missing skills and still pass.
        if breakdown.get("core_gap_passed") is False:
            clauses.append(
                f"Missing core required skills: {breakdown['missing_core_skill_count']} "
                f"exceeds the maximum allowed ({breakdown['max_missing_core_skills']})"
            )
        if breakdown.get("coverage_passed") is False:
            clauses.append(
                f"Required skill coverage {breakdown['mandatory_coverage_pct']}% is below "
                f"the required threshold ({breakdown['required_skill_coverage_threshold']}%)"
            )

        if experience_result is not None and not experience_result["passed"]:
            candidate_years = experience_result["candidate_years"]
            min_years = experience_result["min_years"]
            gap = round(min_years - candidate_years, 1)
            clauses.append(
                f"Insufficient experience: {_format_years(candidate_years)} years provided, "
                f"minimum {_format_years(min_years)} years required (gap: {_format_years(gap)} years)"
            )

        if education_result is not None and not education_result["passed"]:
            required_display = _degree_level_display(education_result["required_level"])
            candidate_display = _degree_level_display(education_result["candidate_level"])
            clauses.append(
                f"Education requirement not met: {required_display} required, {candidate_display} found"
            )

        if not clauses:
            # deterministic_score alone fell below threshold with nothing
            # else to point to (e.g. every mandatory skill matched, but only
            # at low-multiplier hierarchy tiers).
            clauses.append("Deterministic score below threshold")

        reason = " | ".join(clauses) + "."
        if len(reason) > CandidateScoringService.MAX_REJECTION_REASON_LENGTH:
            reason = reason[: CandidateScoringService.MAX_REJECTION_REASON_LENGTH - 1].rstrip() + "…"
        return reason

    def calculate_and_store_score_breakdown(
        self,
        campaign_candidate_id: UUID,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
        experience_result: dict | None = None,
        education_result: dict | None = None,
        score_weights: dict | None = None,
        required_skill_coverage_threshold: float = DEFAULT_REQUIRED_SKILL_COVERAGE_THRESHOLD,
        max_missing_core_skills: int = DEFAULT_MAX_MISSING_CORE_SKILLS,
    ) -> dict:
        """
        Builds the hierarchy-aware mandatory-skill breakdown and persists it
        onto campaign_candidates.score_breakdown, deterministic_score and
        deterministic_passed.

        deterministic_score = (SUM mandatory contributions / SUM max
        mandatory contributions) x 100 - computed entirely in
        build_mandatory_skill_breakdown. mandatory_coverage_pct (matched
        mandatory skill count / total mandatory skill count * 100) is a
        separate, purely informational coverage metric and is never used
        as the deterministic score. Preferred skills never contribute to
        deterministic_score - their EXACT-match bonus is computed and
        stored under score_breakdown.preferred_skill_bonus purely for a
        future Composite Score to consume.

        M07-E02 S04 (Combined Deterministic Score): experience_result/
        education_result are the dicts ExperienceEducationValidationService
        returns - the caller (calculate_deterministic_score_task) computes
        them since it owns the JD/resume fetch, this service never fetches
        either. Omitting both (the default) leaves every pre-existing
        caller's behavior byte-for-byte unchanged - deterministic_score
        stays the pure skill-based ratio, exactly as before M07-E02.

        When either is supplied, deterministic_score becomes a weighted
        blend of the skill/experience/education sub-scores (score_weights,
        default {skills: .70, experience: .15, education: .15} - see
        _DEFAULT_SCORE_WEIGHTS), renormalized across whichever sub-scores
        are "applicable" (a SKIPPED-because-JD-has-no-requirement or
        DATA_MISSING-because-resume-has-no-data sub-score is excluded from
        the blend rather than penalizing or rewarding the candidate for
        something that was never evaluated). The pure skill-only score is
        preserved under score_breakdown.skill_deterministic_score.

        deterministic_passed's skill-stage component is now
        evaluate_skill_qualification's three checks (required-skill
        coverage, importance-weighted skill score vs. deterministic_threshold,
        and missing-CORE-skill count vs. max_missing_core_skills) - NOT a
        bare "no mandatory skill is MISSING" gate anymore. A candidate can
        have missing required skills (core or supporting) and still pass,
        provided coverage/score/core-gap are all within the configured
        limits. See evaluate_skill_qualification for the full rule.

        Flushes via CampaignCandidateRepository.update() but deliberately
        does not commit - that belongs to whatever orchestrates this
        alongside the rest of a candidate's scoring, which does not exist
        yet (Resume Intake integration is explicitly out of scope).
        """
        if self.campaign_candidate_repository is None:
            raise ValueError(
                "CandidateScoringService requires a campaign_candidate_repository "
                "to store a score breakdown."
            )

        campaign_candidate = self.campaign_candidate_repository.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise ValueError(f"CampaignCandidate '{campaign_candidate_id}' not found.")

        breakdown = self.build_mandatory_skill_breakdown(jd_id, resume_id)

        skill_score = breakdown["deterministic_score"]
        # Part 7: the skill stage passes on ALL THREE checks - required
        # skill coverage, the importance-weighted skill score vs.
        # deterministic_threshold, and the missing-CORE-skill limit - never
        # on "no mandatory skill is missing" alone. A candidate CAN have
        # missing required skills (core or supporting) and still pass here.
        # This is the one and only place skill_score is compared against
        # deterministic_threshold for the skill stage itself; the
        # FINAL/blended score (skills + experience + education) below gets
        # its own, separate threshold comparison, exactly as before -
        # comparing skill_score here does not double-validate that one.
        skill_qualification = self.evaluate_skill_qualification(
            breakdown, skill_score, deterministic_threshold,
            required_skill_coverage_threshold, max_missing_core_skills,
        )
        breakdown.update(skill_qualification)

        preferred_breakdown = self.build_preferred_skill_breakdown(jd_id, resume_id)
        breakdown["preferred_skills"] = preferred_breakdown["preferred_skills"]
        breakdown["preferred_skill_bonus"] = preferred_breakdown["preferred_skill_bonus"]

        if experience_result is None and education_result is None:
            # No M07-E02 inputs supplied - skill_score IS the final score in
            # this branch, so skill_qualification_passed (which already
            # includes skill_score >= deterministic_threshold) fully
            # determines final_passed - identical to pre-M07-E02 behavior
            # whenever coverage/core-gap aren't configured to gate anything
            # (their defaults are always-satisfied).
            final_score = skill_score
            final_passed = skill_qualification["skill_qualification_passed"]
        else:
            # Coverage and core-gap are pure skill-stage structural checks
            # (about which/how many required skills are missing) and still
            # gate here regardless of blending. The score-vs-threshold
            # check is deliberately EXCLUDED from this branch's gate -
            # _combine_deterministic_score already validates the campaign
            # threshold exactly once, against the FINAL blended score, per
            # M07-E02's own invariant (a low skill sub-score must be able
            # to clear the bar via a strong experience/education blend,
            # never separately re-checked against skill_score here).
            structural_passed = skill_qualification["coverage_passed"] and skill_qualification["core_gap_passed"]
            final_score, final_passed = self._combine_deterministic_score(
                skill_score, structural_passed, experience_result, education_result,
                score_weights, float(deterministic_threshold),
            )
            breakdown["skill_deterministic_score"] = skill_score
            if experience_result is not None:
                breakdown["experience_validation"] = experience_result
            if education_result is not None:
                breakdown["education_validation"] = education_result

        breakdown["deterministic_threshold"] = float(deterministic_threshold)
        breakdown["deterministic_score"] = final_score
        breakdown["deterministic_passed"] = final_passed

        campaign_candidate.deterministic_breakdown = breakdown
        campaign_candidate.deterministic_score = final_score
        campaign_candidate.deterministic_passed = final_passed
        self.campaign_candidate_repository.update(campaign_candidate)

        return breakdown

    # Renormalized weighted blend across whichever of skills/experience/
    # education are "applicable" (see ExperienceEducationValidationService
    # docstring for what SKIPPED/DATA_MISSING mean) - skills is always
    # applicable (it has no such concept), so this never divides by zero.
    _DEFAULT_SCORE_WEIGHTS = {"skills": 0.70, "experience": 0.15, "education": 0.15}

    def _combine_deterministic_score(
        self, skill_score, mandatory_skills_passed, experience_result, education_result,
        score_weights, deterministic_threshold,
    ) -> tuple[float, bool]:
        weights = score_weights or self._DEFAULT_SCORE_WEIGHTS

        components = [(skill_score, weights["skills"])]
        if experience_result is not None and experience_result["applicable"]:
            components.append((experience_result["score"], weights["experience"]))
        if education_result is not None and education_result["applicable"]:
            components.append((education_result["score"], weights["education"]))

        weight_sum = sum(weight for _, weight in components)
        combined_score = (
            round(sum(score * weight for score, weight in components) / weight_sum, 2)
            if weight_sum > 0 else skill_score
        )

        # The campaign threshold is validated exactly once, here, against
        # combined_score - never separately against skill_score.
        combined_passed = (
            mandatory_skills_passed
            and (experience_result is None or experience_result["passed"])
            and (education_result is None or education_result["passed"])
            and combined_score >= deterministic_threshold
        )
        return combined_score, combined_passed
