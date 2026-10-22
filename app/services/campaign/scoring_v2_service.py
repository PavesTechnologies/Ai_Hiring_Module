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
import hashlib
import logging

from app.models.scoring_v2 import CandidateScoringResult
from app.models.skills import JDSkill, SkillOntology
from app.repositories.provisional_skill_repository import ProvisionalSkillRepository
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.repositories.skill_detection_repository import SkillDetectionRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService, allowed_missing_core, score_threshold_rejects,
)
from app.services.campaign.scoring_v2 import (
    OUTCOME_NOT_RUN, OUTCOME_PENDING, SCORING_V2_CONFIG_KEY, SCORING_VERSION, EvidenceInput, JDSkillInput,
    ProvisionalSkillInput, ScoringInputs, apply_unavailable_outcome, config_version, parse_config, score_v2,
)
from app.services.jd.jd_calibration_rules import jd_profile
from app.services.jd.jd_profile_templates import jd_evidence_sections, jd_paraphrase_sections
from app.services.jd.jd_skill_importance import unknown_skill_tier
from app.services.skills.core_skill_confirmation_service import (
    PROMPT_VERSION, STATUS_CONFIRMED, STATUS_NOT_FOUND, STATUS_UNCERTAIN, CoreSkillConfirmationService,
    jd_core_skill_specs,
)
from app.services.skills.paraphrase_proposal_service import (
    ParaphraseProposalService, ParaphraseTarget, proposals_hash,
)
from app.services.skills.resume_skill_scanner import DETECTION_VERSION
from app.services.skills.skill_evidence_service import (
    SkillEvidenceService, detect_stored_resume, redacted_resume_text,
)

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


def paraphrase_text_hash(text_hash: str, proposals) -> str:
    """The paraphrase round's cache text hash: 64 hex, like every text_hash column."""
    return hashlib.sha256(f"{text_hash}:paraphrase:{proposals_hash(proposals)}".encode("utf-8")).hexdigest()


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

    def provisional_inputs(self, job_description, resume, *, persist: bool = True) -> list[ProvisionalSkillInput]:
        """
        The JD's unresolved unknown skills, found or not in this resume's
        redacted text. Read from the scan detection stored; when that scan
        predates one of them (a JD unknown skill added later), the stored
        resume is scanned again now.
        """
        repo = ProvisionalSkillRepository(self.db)
        pending = repo.pending_for_jd(job_description.id)
        if not pending:
            return []
        scan = repo.get_scan(resume.id)
        scanned = set(scan.scanned_unknown_skill_ids or ()) if scan is not None else set()
        hits = {str(h.get("unknown_skill_id")): h for h in (scan.hits or [])} if scan is not None else {}
        if any(unknown.id not in scanned for _, unknown in pending):
            try:
                text = redacted_resume_text(resume)
                found = self.evidence_service.scan_provisional_skills(resume.id, text, persist=persist)
                hits = {str(k): v for k, v in found.items()}
                scanned = {unknown.id for _, unknown in pending} | scanned
            except Exception:
                logger.exception("Provisional re-scan failed | resume_id=%s", resume.id)
        return [
            ProvisionalSkillInput(
                unknown_skill_id=unknown.id, name=unknown.raw_text,
                tier=unknown_skill_tier(job_description.extracted_json, link, unknown),
                found=str(unknown.id) in hits, excerpt=(hits.get(str(unknown.id)) or {}).get("excerpt"),
                scanned=unknown.id in scanned,
            )
            for link, unknown in pending
        ]

    def _paraphrase_round(self, *, resume, job_description, specs, confirmations: dict, evidence_rows, tools,
                          config: dict, text_hash: str, persist: bool, use_cache: bool) -> tuple[dict, list]:
        """
        Phase 3 Step 6: for core skills still NOT_FOUND / UNCERTAIN, propose
        similar experience lines and ask the 3-vote confirmation again with
        them (DERIVED, never auto-confirmed). Updates `confirmations` in place
        for those skills; returns (paraphrase details, extra EvidenceInput).
        Cached under its own key (text hash + proposals hash).
        """
        skills_by_id = {s.id: s for s in self.db.query(SkillOntology).filter(
            SkillOntology.id.in_([s.canonical_skill_id for s in specs])).all()}
        targets = _paraphrase_targets_for(specs, confirmations, skills_by_id)
        info = {"ran": False, "llm_calls": 0, "targets": [t.name for t in targets], "proposals": []}
        if not targets:
            return info, []
        from app.services.ai.embedding_service import EmbeddingService
        text = redacted_resume_text(resume)
        proposer = ParaphraseProposalService(EmbeddingService(self.db).generate_embeddings, tools.resolver,
                                             tools.blocked)
        proposals, scores = proposer.propose(text, targets, evidence_rows, config,
                                             core_skill_ids=[s.canonical_skill_id for s in specs])
        info["proposals"] = [{"canonical_skill_id": str(p.canonical_skill_id), "line": p.line,
                              "similarity": p.similarity} for p in proposals]
        info["top_similarities"] = {name: scores.get(str(t.canonical_skill_id), [])[:5]
                                    for t, name in ((t, t.name) for t in targets)}
        if not proposals:
            return info, []

        proposed_ids = {p.canonical_skill_id for p in proposals}
        asked = [s for s in specs if s.canonical_skill_id in proposed_ids]
        rows = [{"canonical_skill_id": p.canonical_skill_id, "sense": None, "sources": ["DERIVED"],
                 "evidence": [{"text": p.line, "source": "DERIVED", "section": p.section,
                               "origin": f"paraphrase (similarity {p.similarity:.2f})"}]}
                for p in proposals]
        # Proposals first (the excerpt cap keeps the first few), then what was already found.
        rows += [r for r in evidence_rows if getattr(r, "canonical_skill_id", None) in proposed_ids]
        outcome = CoreSkillConfirmationService(self.provider_resolver(), self.detection_repository).confirm(
            resume_id=resume.id, jd_id=job_description.id, core_skills=asked, evidence_rows=rows,
            text_hash=paraphrase_text_hash(text_hash, proposals), persist=persist,
            budget=self._budget if persist else None, use_cache=use_cache,
        )
        info.update(ran=not outcome.skipped_budget, cached=outcome.cached, llm_calls=outcome.llm_calls,
                    problem=outcome.problem, skipped_budget=outcome.skipped_budget,
                    llm_unavailable=outcome.llm_unavailable, results=outcome.results)
        extra = []
        if outcome.skipped_budget or outcome.llm_unavailable:
            return info, extra
        for result in outcome.results:
            status = result.get("status")
            confirmations[str(result["canonical_skill_id"])] = {**result, "paraphrase": True}
            if status in (STATUS_CONFIRMED, STATUS_UNCERTAIN):
                extra += [EvidenceInput(canonical_skill_id=p.canonical_skill_id, sense=None, sources=("DERIVED",),
                                        best_source="DERIVED", match_tier="PARAPHRASE", recognition=0.8,
                                        evidence_text=p.line[:240])
                          for p in proposals if str(p.canonical_skill_id) == str(result["canonical_skill_id"])]
        return info, extra

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
        evidence_records: list | None = None,
        use_confirmation_cache: bool = True,
    ) -> dict:
        """
        evidence_records / use_confirmation_cache: for comparisons - score this
        parse's own evidence (instead of the stored rows) and ask the model
        afresh instead of reusing a cached confirmation. Pass persist=False.
        """
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
            hr_sense = row.sense_key if isinstance(getattr(row, "sense_key", None), str) else None
            choice = tools.resolver.resolve_jd_sense(row.canonical_skill_id, job_description.raw_text or "", name, jd_title,
                                                     hr_sense=hr_sense)
            jd_skills.append(JDSkillInput(
                canonical_skill_id=row.canonical_skill_id, name=name, mandatory=bool(row.mandatory),
                weight=float(row.weight) if row.weight is not None else None,
                importance=_importance(row) if row.mandatory else None,
                alias_skill_ids=tuple(row.alias_skill_ids or ()), sense=choice.sense, sense_basis=choice.basis,
            ))

        evidence_rows = evidence_records if evidence_records is not None else self.detection_repository.list_evidence(resume.id)
        evidence_source = "given" if evidence_records is not None else "stored"
        if not evidence_rows and evidence_records is None:
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

        descriptions = dict(self.db.query(SkillOntology.id, SkillOntology.description)
                            .filter(SkillOntology.id.in_([r.canonical_skill_id for r in rows])).all()) if rows else {}
        specs = jd_core_skill_specs(job_description, rows, names, tools.resolver, skill_descriptions=descriptions)
        # Phase 3 Step 7: the JD profile template decides which resume sections count as evidence.
        evidence_sections = jd_evidence_sections(job_description)
        confirmation = {"ran": False, "llm_calls": 0}
        confirmations: dict[str, dict] = {}
        special_outcome = None
        if specs and use_llm and self.provider_resolver is not None:
            try:
                outcome = CoreSkillConfirmationService(self.provider_resolver(), self.detection_repository).confirm(
                    resume_id=resume.id, jd_id=job_description.id, core_skills=specs,
                    evidence_rows=evidence_rows, text_hash=resume_text_hash or "", persist=persist,
                    budget=self._budget if persist else None, use_cache=use_confirmation_cache,
                    strong_sections=evidence_sections,
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

        paraphrase = {"ran": False, "llm_calls": 0}
        paraphrase_config = config.get("paraphrase") or {}
        paraphrase_config = {**paraphrase_config,
                             "sections": jd_paraphrase_sections(job_description, paraphrase_config.get("sections"))}
        if (specs and use_llm and self.provider_resolver is not None and special_outcome is None
                and paraphrase_config.get("enabled")):
            try:
                paraphrase, extra_evidence = self._paraphrase_round(
                    resume=resume, job_description=job_description, specs=specs, confirmations=confirmations,
                    evidence_rows=evidence_rows, tools=tools, config=paraphrase_config,
                    text_hash=resume_text_hash or "", persist=persist, use_cache=use_confirmation_cache,
                )
                evidence += extra_evidence
                # Counted with the confirmation calls, so the daily cap sees them.
                confirmation["llm_calls"] = int(confirmation.get("llm_calls") or 0) + paraphrase["llm_calls"]
                if paraphrase.get("llm_unavailable"):
                    special_outcome = (OUTCOME_PENDING, paraphrase.get("problem") or "confirmation LLM unavailable")
            except Exception as exc:
                # The first-round result stands; paraphrase never fails scoring.
                logger.exception("v2 paraphrase round failed | campaign_candidate_id=%s", campaign_candidate.id)
                paraphrase = {"ran": False, "llm_calls": 0, "problem": f"{type(exc).__name__}: {str(exc)[:200]}"}

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
                   "paraphrase": paraphrase, "evidence_source": evidence_source, "acted_on": acted_on,
                   "profile_template": {"key": getattr(jd_profile(job_description), "key", None),
                                        "evidence_sections": sorted(evidence_sections),
                                        "paraphrase_sections": paraphrase_config.get("sections")}},
            provisional_skills=self.provisional_inputs(job_description, resume, persist=persist),
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


def _paraphrase_targets_for(specs, confirmations: dict, skills_by_id: dict) -> list[ParaphraseTarget]:
    targets = []
    for spec in specs:
        status = (confirmations.get(str(spec.canonical_skill_id)) or {}).get("status", STATUS_NOT_FOUND)
        if status not in (STATUS_NOT_FOUND, STATUS_UNCERTAIN):
            continue                       # CONFIRMED / REJECTED / SENSE_MISMATCH (decided) / UNCONFIRMED
        skill = skills_by_id.get(spec.canonical_skill_id)
        targets.append(ParaphraseTarget(
            canonical_skill_id=spec.canonical_skill_id, name=spec.name,
            aliases=tuple(getattr(skill, "aliases", None) or ()), description=getattr(skill, "description", None),
            sense=spec.sense, sense_basis=spec.sense_basis, sense_description=spec.sense_description,
        ))
    return targets


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
