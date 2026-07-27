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

    def build_mandatory_skill_breakdown(
        self,
        jd_id: UUID,
        resume_id: UUID,
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
        """
        if self.skill_ontology_repository is None or self.config_repository is None:
            raise ValueError(
                "CandidateScoringService requires skill_ontology_repository and "
                "config_repository for hierarchy-aware mandatory-skill scoring."
            )

        coverage_rows = self.skill_repository.get_mandatory_skill_coverage(jd_id, resume_id)

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
            ["HIERARCHY_GRANDCHILD_MULTIPLIER", "HIERARCHY_SEMANTIC_ONLY_THRESHOLD"]
        )
        grandchild_multiplier = float(configs.get("HIERARCHY_GRANDCHILD_MULTIPLIER", _DEFAULT_GRANDCHILD_MULTIPLIER))
        # No safe default exists for this one (unlike grandchild's ticket-
        # specified "0.5 or platform_config value") - if it's not
        # configured, the SEMANTIC tier is skipped entirely rather than
        # inventing a threshold, and every otherwise-semantic skill simply
        # falls through to MISSING.
        semantic_threshold_raw = configs.get("HIERARCHY_SEMANTIC_ONLY_THRESHOLD")
        semantic_threshold = float(semantic_threshold_raw) if semantic_threshold_raw is not None else None

        mandatory_skills = []
        matched_count = 0

        for row in coverage_rows:
            entry = self._score_one_mandatory_skill(
                row, candidate_skills_by_id, grandchild_multiplier, semantic_threshold,
            )
            if entry["match_type"] in _COVERED_MATCH_TYPE_VALUES:
                matched_count += 1
            mandatory_skills.append(entry)

        total_mandatory = len(mandatory_skills)
        # mandatory_coverage_pct: display-only, a pure count-based coverage
        # metric ("how many mandatory skills matched at all, regardless of
        # tier quality"). Never used as the deterministic score.
        mandatory_coverage_pct = (
            round((matched_count / total_mandatory) * 100, 2) if total_mandatory > 0 else 100.0
        )

        # deterministic_score = (SUM mandatory contributions / SUM max
        # mandatory contributions) x 100. The "max" contribution for a
        # skill is what it would have contributed on a perfect EXACT match
        # (hierarchy_multiplier=1.0, candidate_scoring_weight=1.0) - i.e.
        # jd_skill.weight itself. This ratio is what makes the score a
        # true 0-100 scale regardless of the actual magnitude JD skill
        # weights happen to use (equal-weight auto-assignment or a future
        # manual override) - no fixed point budget is assumed or required.
        actual_sum = sum(entry["skill_contribution"] or 0 for entry in mandatory_skills)
        max_sum = sum(entry["configured_weight"] or 0 for entry in mandatory_skills)
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
    ) -> dict:
        canonical_skill_id = row.canonical_skill_id
        weight = float(row.weight) if row.weight is not None else None
        mandatory = bool(row.mandatory)

        # Fetched once up front (not just for the SIBLING tier as before) -
        # every entry needs the JD skill's own canonical_name (T03), and
        # SIBLING/SEMANTIC still need target_skill itself below.
        target_skill = self.skill_ontology_repository.get_skill_by_id(canonical_skill_id)
        canonical_name = target_skill.canonical_name if target_skill is not None else None

        # Tier 1: EXACT - already resolved by the T01 LEFT JOIN. The
        # matched candidate skill IS the JD skill itself here.
        if row.candidate_scoring_weight is not None:
            return self._breakdown_entry(
                canonical_skill_id, canonical_name, mandatory, weight,
                MandatorySkillMatchType.EXACT, 1.0,
                float(row.candidate_scoring_weight), row.match_tier, row.confidence,
                matched_candidate_skill_canonical_name=canonical_name,
            )

        # Tier 2: CHILD (depth 1). Inactive (deactivated/deprecated) skills
        # are never valid hierarchy match targets (S03-T01), even if a
        # stale candidate_skills row still points at one.
        children = [
            child for child in self.skill_ontology_repository.get_children(canonical_skill_id)
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
            )

        # Tier 3: GRANDCHILD (depth 2) - only reached because no direct
        # child matched above, so direct child always takes precedence.
        if _MAX_HIERARCHY_DEPTH >= 2:
            grandchildren = [
                grandchild
                for child in children
                for grandchild in self.skill_ontology_repository.get_children(child.id)
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
                for sibling in self.skill_ontology_repository.get_children(target_skill.parent_skill_id)
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
                    )
                    entry["semantic_similarity"] = round(similarity, 4)
                    return entry

        # MISSING - nothing matched at any tier.
        entry = self._breakdown_entry(
            canonical_skill_id, canonical_name, mandatory, weight,
            MandatorySkillMatchType.MISSING, 0.0, None, None, None,
            matched_candidate_skill_canonical_name=None,
        )
        if sibling_skip_reason is not None:
            entry["sibling_skip_reason"] = sibling_skip_reason
        return entry

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
    ) -> dict:
        if match_type == MandatorySkillMatchType.MISSING:
            # Requirement: a final unmatched skill always contributes 0,
            # even when weight itself is unset.
            contribution = 0.0
        elif weight is None or candidate_scoring_weight is None:
            # Can't compute jd_skill.weight * candidate_skill.scoring_weight
            # * hierarchy_score_multiplier without both factors - reported
            # as unknown rather than silently defaulted.
            contribution = None
        else:
            contribution = round(weight * candidate_scoring_weight * hierarchy_score_multiplier, 4)

        return {
            "canonical_skill_id": str(canonical_skill_id),
            "canonical_name": canonical_name,
            "mandatory": mandatory,
            "configured_weight": weight,
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

        preferred_skills = []
        for row in coverage_rows:
            weight = float(row.weight) if row.weight is not None else None
            is_exact_match = row.candidate_scoring_weight is not None
            target_skill = self.skill_ontology_repository.get_skill_by_id(row.canonical_skill_id)
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

    def calculate_and_store_score_breakdown(
        self,
        campaign_candidate_id: UUID,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
        experience_result: dict | None = None,
        education_result: dict | None = None,
        score_weights: dict | None = None,
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

        deterministic_passed = (no mandatory skill is MISSING) AND
        (deterministic_score >= deterministic_threshold). A campaign
        requiring 3 mandatory skills must not pass a candidate missing one
        of them just because the other two's weighted contribution clears
        the threshold.

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

        any_missing = any(
            skill["match_type"] == MandatorySkillMatchType.MISSING.value
            for skill in breakdown["mandatory_skills"]
        )
        skill_score = breakdown["deterministic_score"]
        # Mandatory-skill gate only - never compare skill_score to
        # deterministic_threshold here. The threshold is validated exactly
        # once (Step 5/6 of the ATS spec), against the FINAL combined score
        # (skills + experience + education) below. Comparing skill_score to
        # it here too would double-validate the same threshold and could
        # fail a candidate whose skill sub-score alone is under threshold
        # even though their blended score clears it.
        mandatory_skills_passed = not any_missing

        preferred_breakdown = self.build_preferred_skill_breakdown(jd_id, resume_id)
        breakdown["preferred_skills"] = preferred_breakdown["preferred_skills"]
        breakdown["preferred_skill_bonus"] = preferred_breakdown["preferred_skill_bonus"]

        if experience_result is None and education_result is None:
            # No M07-E02 inputs supplied - skill_score IS the final score in
            # this branch, so this is the one and only threshold comparison
            # here, not a second one - identical to pre-M07-E02 behavior.
            final_score = skill_score
            final_passed = mandatory_skills_passed and final_score >= float(deterministic_threshold)
        else:
            final_score, final_passed = self._combine_deterministic_score(
                skill_score, mandatory_skills_passed, experience_result, education_result,
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

        campaign_candidate.score_breakdown = breakdown
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
