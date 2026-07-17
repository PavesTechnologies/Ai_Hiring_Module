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
        Calculate deterministic score based on mandatory JD skills
        matched against candidate normalized skills.

        Score = matched mandatory skills / total mandatory skills * 100
        """

        # Get mandatory JD skills
        mandatory_jd_skills = (
            self.skill_repository.get_mandatory_jd_skills(jd_id)
        )

        # Get candidate normalized skills
        candidate_skills = (
            self.skill_repository.get_candidate_normalized_skills(resume_id)
        )

        # No mandatory skills configured
        if not mandatory_jd_skills:
            return 100.0, True

        jd_skill_ids = {
            skill.canonical_skill_id
            for skill in mandatory_jd_skills
        }

        candidate_skill_ids = {
            skill.canonical_skill_id
            for skill in candidate_skills
            if skill.canonical_skill_id is not None
        }

        matched_skill_ids = jd_skill_ids.intersection(
            candidate_skill_ids
        )

        score = (
            len(matched_skill_ids)
            / len(jd_skill_ids)
        ) * 100

        score = round(score, 2)

        passed = score >= float(deterministic_threshold)

        return score, passed

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
        above does not need either and is unaffected by their absence.
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
        mandatory_coverage_pct = (
            round((matched_count / total_mandatory) * 100, 2) if total_mandatory > 0 else 100.0
        )

        return {
            "mandatory_skills": mandatory_skills,
            "mandatory_coverage_pct": mandatory_coverage_pct,
            "semantic_tier_available": semantic_threshold is not None,
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

        # Tier 1: EXACT - already resolved by the T01 LEFT JOIN.
        if row.candidate_scoring_weight is not None:
            return self._breakdown_entry(
                canonical_skill_id, weight, MandatorySkillMatchType.EXACT, 1.0,
                float(row.candidate_scoring_weight), row.match_tier, row.confidence,
            )

        # Tier 2: CHILD (depth 1).
        children = self.skill_ontology_repository.get_children(canonical_skill_id)
        child_match = self._best_hierarchy_match(children, candidate_skills_by_id)
        if child_match is not None:
            candidate_skill = child_match
            return self._breakdown_entry(
                canonical_skill_id, weight, MandatorySkillMatchType.CHILD, 0.7,
                float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
            )

        # Tier 3: GRANDCHILD (depth 2) - only reached because no direct
        # child matched above, so direct child always takes precedence.
        if _MAX_HIERARCHY_DEPTH >= 2:
            grandchildren = [
                grandchild
                for child in children
                for grandchild in self.skill_ontology_repository.get_children(child.id)
            ]
            grandchild_match = self._best_hierarchy_match(grandchildren, candidate_skills_by_id)
            if grandchild_match is not None:
                candidate_skill = grandchild_match
                return self._breakdown_entry(
                    canonical_skill_id, weight, MandatorySkillMatchType.GRANDCHILD, grandchild_multiplier,
                    float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                )

        # Tier 4: SIBLING - only if no exact/child/grandchild match.
        target_skill = self.skill_ontology_repository.get_skill_by_id(canonical_skill_id)
        sibling_skip_reason = None

        if target_skill is None:
            sibling_skip_reason = "SIBLING_SKIPPED"
        elif target_skill.parent_skill_id is None:
            sibling_skip_reason = "ROOT_SKILL_NO_PARENT"
        else:
            siblings = [
                sibling
                for sibling in self.skill_ontology_repository.get_children(target_skill.parent_skill_id)
                if sibling.id != canonical_skill_id
            ]
            sibling_match = self._best_hierarchy_match(siblings, candidate_skills_by_id)
            if sibling_match is not None:
                candidate_skill = sibling_match
                return self._breakdown_entry(
                    canonical_skill_id, weight, MandatorySkillMatchType.SIBLING, 0.4,
                    float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
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
                    entry = self._breakdown_entry(
                        canonical_skill_id, weight, MandatorySkillMatchType.SEMANTIC, 0.2,
                        float(candidate_skill.scoring_weight), candidate_skill.match_tier, candidate_skill.confidence,
                    )
                    entry["semantic_similarity"] = round(similarity, 4)
                    return entry

        # MISSING - nothing matched at any tier.
        entry = self._breakdown_entry(
            canonical_skill_id, weight, MandatorySkillMatchType.MISSING, 0.0, None, None, None,
        )
        if sibling_skip_reason is not None:
            entry["sibling_skip_reason"] = sibling_skip_reason
        return entry

    @staticmethod
    def _best_hierarchy_match(ontology_skills: list, candidate_skills_by_id: dict):
        """
        Among ontology_skills (children/grandchildren/siblings of the
        mandatory skill), returns the candidate's matching CandidateSkill
        row with the highest scoring_weight, or None if the candidate has
        none of them.
        """
        matches = [
            candidate_skills_by_id[skill.id]
            for skill in ontology_skills
            if skill.id in candidate_skills_by_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda candidate_skill: float(candidate_skill.scoring_weight))

    @staticmethod
    def _breakdown_entry(
        canonical_skill_id: UUID,
        weight: float | None,
        match_type: MandatorySkillMatchType,
        hierarchy_score_multiplier: float,
        candidate_scoring_weight: float | None,
        match_tier: str | None,
        confidence: float | None,
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
            "weight": weight,
            "match_type": match_type.value,
            "hierarchy_score_multiplier": hierarchy_score_multiplier,
            "candidate_scoring_weight": candidate_scoring_weight,
            "match_tier": match_tier,
            "confidence": confidence,
            "contribution": contribution,
        }

    def calculate_and_store_score_breakdown(
        self,
        campaign_candidate_id: UUID,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
    ) -> dict:
        """
        Builds the hierarchy-aware mandatory-skill breakdown and persists
        it onto campaign_candidates.score_breakdown, deterministic_score
        (= mandatory_coverage_pct) and deterministic_passed.

        deterministic_passed is FALSE whenever any mandatory skill is
        ultimately MISSING, regardless of the overall coverage percentage,
        per the finalized rule - a campaign requiring 3 mandatory skills
        at an 50% threshold must not pass a candidate missing one of them
        just because 2-of-3 clears that bar.

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
        deterministic_passed = (
            breakdown["mandatory_coverage_pct"] >= float(deterministic_threshold) and not any_missing
        )

        breakdown["deterministic_threshold"] = float(deterministic_threshold)
        breakdown["deterministic_passed"] = deterministic_passed

        campaign_candidate.score_breakdown = breakdown
        campaign_candidate.deterministic_score = breakdown["mandatory_coverage_pct"]
        campaign_candidate.deterministic_passed = deterministic_passed
        self.campaign_candidate_repository.update(campaign_candidate)

        return breakdown
