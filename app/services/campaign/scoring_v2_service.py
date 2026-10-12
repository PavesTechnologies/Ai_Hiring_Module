"""
Scoring v2 (Phase 1) orchestration for one campaign candidate: gathers the
inputs score_v2() needs, runs the cached core-skill confirmation, scores,
and stores the result in candidate_scoring_results with the exact config
used. Never writes campaign_candidates - acting on the result is the
caller's decision (deterministic_scoring_tasks, by campaign flag).

Which campaigns compute v2 at all:
  - scoring_v2_enabled campaigns (v2 decides),
  - scoring_v2_shadow campaigns (opt-in per campaign: stored, never acted on).

LLM budget: platform_config SCORING_V2_DAILY_LLM_CALL_CAP caps the core
confirmation calls per day (counted from today's stored v2 results). When
the next confirmation would exceed it, v2 is skipped for that candidate and
stored as NOT_RUN; v1 is never affected.

Outcomes beyond PASS / REJECT: PENDING when the confirmation couldn't run
(quota, provider down, backup failing too) - the caller retries it; it is
never turned into a REJECT. NOT_RUN when the daily cap stopped it.
"""
import logging

from app.models.scoring_v2 import CandidateScoringResult
from app.models.skills import JDSkill, SkillOntology
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.repositories.skill_detection_repository import SkillDetectionRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService, allowed_missing_core, score_threshold_rejects,
)
from app.services.campaign.scoring_v2 import (
    OUTCOME_NOT_RUN, OUTCOME_PENDING, SCORING_V2_CONFIG_KEY, SCORING_VERSION, EvidenceInput, JDSkillInput,
    ScoringInputs, apply_unavailable_outcome, config_version, parse_config, score_v2,
)
from app.services.skills.core_skill_confirmation_service import (
    PROMPT_VERSION, CoreSkillConfirmationService, jd_core_skill_specs,
)
from app.services.skills.resume_skill_scanner import DETECTION_VERSION
from app.services.skills.skill_evidence_service import SkillEvidenceService, detect_stored_resume

logger = logging.getLogger(__name__)

DAILY_LLM_CALL_CAP_KEY = "SCORING_V2_DAILY_LLM_CALL_CAP"
DEFAULT_DAILY_LLM_CALL_CAP = 300


def v2_should_run(campaign, config_repository=None) -> bool:
    """v2 runs only on campaigns that opted in: scoring_v2_enabled or the scoring_v2_shadow switch."""
    return (getattr(campaign, "scoring_v2_enabled", False) is True
            or getattr(campaign, "scoring_v2_shadow", False) is True)


def daily_llm_call_cap(config_repository) -> int:
    raw = config_repository.get_configs_by_keys([DAILY_LLM_CALL_CAP_KEY]).get(DAILY_LLM_CALL_CAP_KEY)
    try:
        return max(int(raw), 0) if raw is not None else DEFAULT_DAILY_LLM_CALL_CAP
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LLM_CALL_CAP


def _importance(row) -> str | None:
    value = getattr(getattr(row, "importance", None), "value", getattr(row, "importance", None))
    return str(value).lower() if value else None


class ScoringV2Service:
    def __init__(self, db, skill_repository, config_repository, *, provider_resolver=None, cache_service=None):
        """provider_resolver: () -> LLMProvider for the core confirmation; None = confirmation not run."""
        self.db = db
        self.skill_repository = skill_repository
        self.config_repository = config_repository
        self.provider_resolver = provider_resolver
        self.detection_repository = SkillDetectionRepository(db)
        self.evidence_service = SkillEvidenceService(self.detection_repository, skill_repository, cache_service)

    def _budget(self, calls_needed: int) -> bool:
        """Daily LLM cap: True when calls_needed more confirmation calls still fit in today's cap."""
        cap = daily_llm_call_cap(self.config_repository)
        return ScoringResultRepository(self.db).v2_llm_calls_today() + calls_needed <= cap

    def load_config(self) -> dict:
        return parse_config(self.config_repository.get_configs_by_keys([SCORING_V2_CONFIG_KEY]).get(SCORING_V2_CONFIG_KEY))

    def score(
        self,
        *,
        campaign,
        campaign_candidate,
        job_description,
        resume,
        experience_result: dict | None,
        education_result: dict | None,
        domain_result: dict | None,
        acted_on: bool,
        use_llm: bool = True,
        persist: bool = True,
    ) -> dict:
        config = self.load_config()
        tools = self.evidence_service.load_tools()

        rows = self.db.query(JDSkill).filter(JDSkill.jd_id == job_description.id).all()
        names = dict(self.db.query(SkillOntology.id, SkillOntology.canonical_name)
                     .filter(SkillOntology.id.in_([r.canonical_skill_id for r in rows])).all()) if rows else {}
        jd_title = " ".join(str(v) for v in (getattr(job_description, "title", ""),
                                              (job_description.extracted_json or {}).get("domain")) if v)
        jd_skills = []
        for row in rows:
            name = names.get(row.canonical_skill_id, str(row.canonical_skill_id))
            choice = tools.resolver.resolve_jd_sense(row.canonical_skill_id, job_description.raw_text or "", name, jd_title)
            jd_skills.append(JDSkillInput(
                canonical_skill_id=row.canonical_skill_id, name=name, mandatory=bool(row.mandatory),
                weight=float(row.weight) if row.weight is not None else None,
                importance=_importance(row) if row.mandatory else None,
                alias_skill_ids=tuple(row.alias_skill_ids or ()), sense=choice.sense, sense_basis=choice.basis,
            ))

        evidence_rows = self.detection_repository.list_evidence(resume.id)
        evidence_source = "stored"
        if not evidence_rows:
            records, _ = detect_stored_resume(self.evidence_service, resume, persist=persist)
            evidence_rows = self.detection_repository.list_evidence(resume.id) if persist else records
            evidence_source = "detected now from the stored resume"
        evidence = [
            EvidenceInput(
                canonical_skill_id=r.canonical_skill_id, sense=r.sense, sources=tuple(r.sources or ()),
                best_source=r.best_source, match_tier=r.match_tier, recognition=float(r.recognition),
                evidence_text=((r.evidence or [{}])[0].get("text") or "")[:240],
            )
            for r in evidence_rows
        ]
        resume_text_hash = next((getattr(r, "text_hash", None) for r in evidence_rows if getattr(r, "text_hash", None)), None)

        specs = jd_core_skill_specs(job_description, rows, names, tools.resolver)
        confirmation = {"ran": False, "llm_calls": 0}
        confirmations: dict[str, dict] = {}
        special_outcome = None
        if specs and use_llm and self.provider_resolver is not None:
            try:
                outcome = CoreSkillConfirmationService(self.provider_resolver(), self.detection_repository).confirm(
                    resume_id=resume.id, jd_id=job_description.id, core_skills=specs,
                    evidence_rows=evidence_rows, text_hash=resume_text_hash or "", persist=persist,
                    budget=self._budget if persist else None,
                )
                confirmations = {r["canonical_skill_id"]: r for r in outcome.results}
                confirmation = {"ran": not outcome.skipped_budget, "cached": outcome.cached,
                                "problem": outcome.problem, "llm_calls": outcome.llm_calls,
                                "llm": outcome.call.as_dict() if outcome.call else None,
                                "results": outcome.results}
                if outcome.skipped_budget:
                    special_outcome = (OUTCOME_NOT_RUN, "daily LLM call cap reached")
                elif outcome.llm_unavailable:
                    special_outcome = (OUTCOME_PENDING, outcome.problem or "confirmation LLM unavailable")
            except Exception as exc:
                # Provider couldn't even be resolved/called: no decision on
                # partial information - PENDING, retried by the caller.
                logger.exception("v2 core confirmation failed | campaign_candidate_id=%s", campaign_candidate.id)
                problem = f"{type(exc).__name__}: {str(exc)[:200]}"
                confirmation = {"ran": False, "problem": problem, "llm_calls": 0}
                special_outcome = (OUTCOME_PENDING, problem)

        core_count = sum(1 for s in jd_skills if s.mandatory and s.importance == "core")
        allowed = allowed_missing_core(
            core_count, int(getattr(campaign, "max_missing_core_skills", 3) or 3),
            float(campaign.max_missing_core_pct) if getattr(campaign, "max_missing_core_pct", None) is not None else None,
        )
        snapshot = {
            "scoring_version": SCORING_VERSION,
            "config": config,
            "campaign": {
                "deterministic_threshold": float(campaign.deterministic_threshold),
                "required_skill_coverage_threshold": float(campaign.required_skill_coverage_threshold),
                "max_missing_core_skills": int(getattr(campaign, "max_missing_core_skills", 3) or 3),
                "max_missing_core_pct": float(campaign.max_missing_core_pct)
                if getattr(campaign, "max_missing_core_pct", None) is not None else None,
            },
            "score_threshold_rejects_config": score_threshold_rejects(self.config_repository),
            "detection_version": DETECTION_VERSION,
            "confirmation_prompt_version": PROMPT_VERSION,
        }
        version = config_version(snapshot)

        hierarchy = CandidateScoringService(
            self.skill_repository, SkillOntologyRepository(self.db),
        ).load_mandatory_skill_hierarchy([s.canonical_skill_id for s in jd_skills if s.mandatory])

        breakdown = score_v2(ScoringInputs(
            jd_skills=jd_skills, evidence=evidence, confirmations=confirmations, hierarchy=hierarchy,
            experience_result=experience_result, education_result=education_result, domain_result=domain_result,
            deterministic_threshold=float(campaign.deterministic_threshold),
            required_skill_coverage_threshold=float(campaign.required_skill_coverage_threshold),
            allowed_missing_core=allowed,
            score_threshold_rejects=snapshot["score_threshold_rejects_config"],
            extra={"scoring_config_version": version, "confirmation": confirmation,
                   "evidence_source": evidence_source, "acted_on": acted_on},
        ), config)
        if special_outcome is not None:
            apply_unavailable_outcome(breakdown, *special_outcome)

        if persist:
            ScoringResultRepository(self.db).add(CandidateScoringResult(
                campaign_candidate_id=campaign_candidate.id, scoring_version=SCORING_VERSION,
                score=breakdown["deterministic_score"], passed=breakdown["deterministic_passed"],
                outcome=breakdown["outcome"], breakdown=breakdown, scoring_config_version=version,
                config_snapshot=snapshot, acted_on=acted_on,
            ))
        return breakdown


def v1_result_row(campaign, campaign_candidate, breakdown: dict, *, acted_on: bool, score_weights: dict | None):
    """candidate_scoring_results row for the v1 scoring that just ran, with its config snapshot."""
    snapshot = {
        "scoring_version": "v1",
        "score_weights": score_weights,
        "deterministic_threshold": float(campaign.deterministic_threshold),
        "required_skill_coverage_threshold": float(campaign.required_skill_coverage_threshold),
        "max_missing_core_skills": breakdown.get("max_missing_core_skills"),
        "max_missing_core_pct": breakdown.get("max_missing_core_pct"),
        "allowed_missing_core_skills": breakdown.get("allowed_missing_core_skills"),
        "score_threshold_rejects": breakdown.get("score_threshold_rejects"),
    }
    return CandidateScoringResult(
        campaign_candidate_id=campaign_candidate.id, scoring_version="v1",
        score=breakdown.get("deterministic_score"), passed=breakdown.get("deterministic_passed"),
        outcome=("HOLD" if breakdown.get("needs_review") else "PASS") if breakdown.get("deterministic_passed")
        else "REJECT",
        breakdown=breakdown, scoring_config_version=config_version(snapshot), config_snapshot=snapshot,
        acted_on=acted_on,
    )


def v2_rejection_reason(breakdown: dict) -> str:
    """Plain-language reason when v2 rejects (used only when v2 decides)."""
    parts = []
    for check in breakdown.get("checks", []):
        if not check["passed"]:
            parts.append(f"{check['check']}: {check.get('detail') or 'not met'}")
    if breakdown.get("core_rejected_by_llm"):
        parts.append("Not shown on the resume: " + ", ".join(breakdown["core_rejected_by_llm"]))
    return " | ".join(parts)[:500] or "Did not meet the deterministic requirements."
