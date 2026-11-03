"""
Scoring v2 (Phase 1) orchestration for one campaign candidate: gathers the
inputs score_v2() needs, runs the cached core-skill confirmation, scores,
and stores the result in candidate_scoring_results with the exact config
used. Never writes campaign_candidates - acting on the result is the
caller's decision (deterministic_scoring_tasks, by campaign flag).

Which scoring version: the current one (scoring_versions) decides every campaign since retire v1,
phase C/D; a campaign trying a candidate version also has it scored - stored, never acted on
(scoring_v2_tasks). The version's configuration and model are used, and recorded on the result.

LLM budget: platform_config SCORING_V2_DAILY_LLM_CALL_CAP caps the core
confirmation calls per day (counted from today's stored v2 results). When
the next confirmation would exceed it, v2 is skipped for that candidate and
stored as NOT_RUN; v1 is never affected.

Outcomes beyond PASS / REJECT: PENDING when the confirmation couldn't run
(quota, provider down, backup failing too) - the caller retries it; it is
never turned into a REJECT. NOT_RUN when the daily cap stopped it.
"""
import hashlib
import json
import logging

from app.models.scoring_v2 import CandidateScoringResult
from app.models.skills import JDSkill, SkillOntology
from app.repositories.provisional_skill_repository import ProvisionalSkillRepository
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.repositories.skill_detection_repository import SkillDetectionRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.services.campaign.scoring_v2 import (
    OUTCOME_NOT_RUN, OUTCOME_PENDING, REASON_AWAITING_FULL_SCORING, SCORING_MODE_LLM, SCORING_VERSION,
    EvidenceInput, JDSkillInput, ProvisionalSkillInput, ScoringInputs, allowed_missing_core, apply_no_llm_outcome,
    apply_unavailable_outcome, config_version, merge_config, score_v2,
)

# ScoringV2Service.score(llm_fallback=...): when the core-skill votes can't be had.
FALLBACK_NEVER = "never"
FALLBACK_ON_CAP = "on_cap"
FALLBACK_ON_UNAVAILABLE = "on_unavailable"
FALLBACK_ALWAYS = "always"
NO_LLM_ON_REQUEST = "scored without the LLM on request"
from app.services.jd.jd_calibration_rules import jd_profile, jd_role_domain
from app.services.jd.jd_profile_templates import (
    jd_auto_confirm_sections, jd_evidence_sections, jd_listed_only_can_confirm, jd_paraphrase_sections,
)
from app.services.jd.jd_role_domain import suggest_role_domain
from app.services.jd.jd_skill_importance import unknown_skill_tier
from app.services.skills.core_skill_confirmation_service import (
    PROMPT_VERSION, STATUS_CONFIRMED, STATUS_NOT_FOUND, STATUS_REJECTED, STATUS_UNCERTAIN,
    CoreSkillConfirmationService, JobContext, jd_core_skill_specs,
)
from app.services.skills.paraphrase_proposal_service import (
    ParaphraseProposalService, ParaphraseTarget, proposals_hash,
)
from app.services.llm.call_log import calls_in_quota_day, llm_call_source, llm_call_subject
from app.services.skills.evidence_rules import role_for_line, roles_of
from app.services.skills.resume_sections import SECTION_EXPERIENCE
from app.services.skills.name_mask import NameMask
from app.services.skills.resume_skill_scanner import DETECTION_VERSION
from app.services.skills.skill_evidence_service import (
    CERTIFICATION_ORIGIN, SkillEvidenceService, detect_stored_resume, redacted_resume_text,
)

logger = logging.getLogger(__name__)

DAILY_LLM_CALL_CAP_KEY = "SCORING_V2_DAILY_LLM_CALL_CAP"
DEFAULT_DAILY_LLM_CALL_CAP = 300



# Whether a deterministic score below campaign.deterministic_threshold
# rejects on its own for a JD that HAS core skills (a JD without any keeps
# the score gate - it's the only skill gate it has). Off by default: the
# score is built mostly from
# supporting skills/competencies ("Change Management", "Responsible AI"...)
# that strong senior candidates rarely list by name, so on its own it
# filtered out good candidates before semantic/AI evaluation ever saw them.
# Hard requirements still reject: too many missing CORE skills, required-
# skill coverage, experience and education. The score still ranks and feeds
# the composite. Set platform_config DETERMINISTIC_SCORE_REJECTS=true to
# restore score-threshold rejection.
DETERMINISTIC_SCORE_REJECTS_KEY = "DETERMINISTIC_SCORE_REJECTS"


def score_threshold_rejects(config_repository) -> bool:
    if config_repository is None:
        return False
    value = config_repository.get_configs_by_keys([DETERMINISTIC_SCORE_REJECTS_KEY]).get(DETERMINISTIC_SCORE_REJECTS_KEY)
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")

def load_mandatory_skill_hierarchy(skill_ontology_repository, mandatory_skill_ids: list) -> dict:
    """
    Everything the CHILD/GRANDCHILD/SIBLING hierarchy tiers need for a fixed set of mandatory skill ids,
    in 3 queries (the skills, their children/siblings, their grandchildren). Depends only on the JD's
    mandatory skills, never on the resume.
    """
    skills_by_id = skill_ontology_repository.get_skills_by_ids(mandatory_skill_ids)
    parent_ids = {skill.parent_skill_id for skill in skills_by_id.values() if skill.parent_skill_id is not None}
    children_by_parent = skill_ontology_repository.get_children_batch(list(set(mandatory_skill_ids) | parent_ids))
    child_ids = {
        child.id
        for skill_id in mandatory_skill_ids
        for child in children_by_parent.get(skill_id, [])
        if child.is_active
    }
    grandchildren_by_parent = skill_ontology_repository.get_children_batch(list(child_ids))
    return {
        "skills_by_id": skills_by_id,
        "children_by_parent": children_by_parent,
        "grandchildren_by_parent": grandchildren_by_parent,
    }

def _cap_value(value) -> int | None:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def daily_llm_call_caps(config_repository) -> dict[str, int]:
    """
    Per-model daily caps (the provider's free-tier limit is per model):
    SCORING_V2_DAILY_LLM_CALL_CAP is a JSON object {"default": 400,
    "gemini-3.1-flash-lite": 400, ...}, or a plain number for every model.
    Always has a "default" entry.
    """
    raw = config_repository.get_configs_by_keys([DAILY_LLM_CALL_CAP_KEY]).get(DAILY_LLM_CALL_CAP_KEY)
    plain = _cap_value(raw)
    if plain is not None:
        return {"default": plain}
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        parsed = {}
    caps = {str(model): cap for model, value in (parsed.items() if isinstance(parsed, dict) else [])
            if (cap := _cap_value(value)) is not None}
    caps.setdefault("default", DEFAULT_DAILY_LLM_CALL_CAP)
    return caps


def cap_for_model(caps: dict[str, int], model: str | None) -> int:
    return caps.get(model, caps["default"]) if model else caps["default"]


def daily_llm_call_cap(config_repository) -> int:
    """The default cap (models without their own entry)."""
    return daily_llm_call_caps(config_repository)["default"]


def provider_models(provider) -> list[str]:
    """The models a provider can call: its main model, then its backup (FallbackLLMProvider)."""
    models = [getattr(provider, "model", None)]
    fallback = getattr(provider, "fallback", None)
    models.append(getattr(fallback, "model", None) if fallback is not None else None)
    return [m for m in dict.fromkeys(models) if isinstance(m, str) and m]


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
        """
        Daily LLM cap, per model: True when calls_needed more confirmation
        calls still fit under the cap of the main model or of its backup.
        Counts every LLM request logged for that model in the provider's
        quota day (llm_call_log - parsing, AI evaluation, dry runs too).
        """
        caps = daily_llm_call_caps(self.config_repository)
        try:
            models = provider_models(self.provider_resolver()) if self.provider_resolver else []
        except Exception:
            models = []
        if not models:
            return calls_in_quota_day(self.db) + calls_needed <= caps["default"]
        return any(calls_in_quota_day(self.db, model=model) + calls_needed <= cap_for_model(caps, model)
                   for model in models)

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
                          config: dict, text_hash: str, persist: bool, use_cache: bool,
                          name_mask: NameMask | None = None) -> tuple[dict, list]:
        """
        Phase 3 Step 6: for core skills still NOT_FOUND / UNCERTAIN, propose
        similar experience lines and ask the 3-vote confirmation again with
        them (DERIVED, never auto-confirmed). Updates `confirmations` in place
        for those skills; returns (paraphrase details, extra EvidenceInput).
        Cached under its own key (text hash + proposals hash).
        """
        skills_by_id = {s.id: s for s in self.db.query(SkillOntology).filter(
            SkillOntology.id.in_([s.canonical_skill_id for s in specs])).all()}
        targets = _paraphrase_targets_for(specs, confirmations, skills_by_id, evidence_rows)
        info = {"ran": False, "llm_calls": 0, "targets": [t.name for t in targets], "proposals": []}
        if not targets:
            return info, []
        from app.services.ai.embedding_service import EmbeddingService
        text = redacted_resume_text(resume)
        proposer = ParaphraseProposalService(EmbeddingService(self.db).generate_embeddings, tools.resolver,
                                             tools.blocked)
        from app.services.skills.skill_evidence_service import job_titles_of
        proposals, scores = proposer.propose(text, targets, evidence_rows, config,
                                             core_skill_ids=[s.canonical_skill_id for s in specs],
                                             exclude_line=tools.rules.qualified,
                                             job_titles=job_titles_of(getattr(resume, "parsed_json", None)))
        info["proposals"] = [{"canonical_skill_id": str(p.canonical_skill_id), "line": p.line,
                              "similarity": p.similarity} for p in proposals]
        info["top_similarities"] = {name: scores.get(str(t.canonical_skill_id), [])[:5]
                                    for t, name in ((t, t.name) for t in targets)}
        if not proposals:
            return info, []

        proposed_ids = {p.canonical_skill_id for p in proposals}
        asked = [s for s in specs if s.canonical_skill_id in proposed_ids]
        lines, roles = text.splitlines(), roles_of(getattr(resume, "parsed_json", None) or {})

        def proposed(p):
            item = {"text": p.line, "source": "DERIVED", "section": p.section,
                    "origin": f"paraphrase (similarity {p.similarity:.2f})"}
            # The voters see the role the line belongs to, like any experience excerpt.
            role = role_for_line(lines, p.line_no, roles) if p.section == SECTION_EXPERIENCE else None
            return {**item, "role": role} if role else item

        rows = [{"canonical_skill_id": p.canonical_skill_id, "sense": None, "sources": ["DERIVED"],
                 "evidence": [proposed(p)]} for p in proposals]
        # Proposals first (the excerpt cap keeps the first few), then what was already found.
        rows += [r for r in evidence_rows if getattr(r, "canonical_skill_id", None) in proposed_ids]
        outcome = CoreSkillConfirmationService(self.provider_resolver(), self.detection_repository).confirm(
            resume_id=resume.id, jd_id=job_description.id, core_skills=asked, evidence_rows=rows,
            text_hash=paraphrase_text_hash(text_hash, proposals), persist=persist,
            budget=self._budget if persist else None, use_cache=use_cache,
            rules=tools.rules, job=job_context(job_description, [getattr(s, "name", None) for s in specs]),
            cap_listed_only_skills=not jd_listed_only_can_confirm(job_description), name_mask=name_mask,
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
        """The current scoring version's configuration; the code's defaults before one exists (logged)."""
        from app.services.campaign.scoring_versions import current_version, version_config
        version = current_version(self.db)
        if version is None:
            logger.warning("No current scoring version - scoring with the code's default configuration "
                           "(run the scoring seed).")
            return merge_config(None)
        return version_config(version)

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
        llm_fallback: str = FALLBACK_NEVER,
        scoring_version=None,
    ) -> dict:
        """
        llm_fallback: when the core-skill votes can't be had, score without the LLM (the scan,
        auto-confirm and rules only - apply_no_llm_outcome) instead of returning NOT_RUN /
        PENDING: "never" (shadow, comparisons), "on_cap" (the daily cap only), "on_unavailable"
        (the cap or a provider failure - the deciding path's last attempt), "always" (score
        without the LLM outright: the golden set's --no-llm run).

        evidence_records / use_confirmation_cache: for comparisons - score this
        parse's own evidence (instead of the stored rows) and ask the model
        afresh instead of reusing a cached confirmation. Pass persist=False.
        The LLM calls made while scoring are logged against this resume and
        campaign (llm_call_subject).

        scoring_version: the version to score with (scoring_versions); None = the current one. Its
        configuration and its model (when set) run the core confirmation; the result records it
        (scoring_version_id). With no version at all (a fresh database before its seed), the code's
        default configuration and the platform's model.
        """
        from app.services.campaign.scoring_versions import current_version, provider_resolver_for
        version = scoring_version if scoring_version is not None else current_version(self.db)
        default_resolver = self.provider_resolver
        self.provider_resolver = provider_resolver_for(self.db, version, default_resolver)
        with llm_call_subject(resume_id=getattr(resume, "id", None), campaign_id=getattr(campaign, "id", None)):
            try:
                return self._score(campaign=campaign, campaign_candidate=campaign_candidate,
                                   job_description=job_description, resume=resume,
                                   experience_result=experience_result, education_result=education_result,
                                   domain_result=domain_result, acted_on=acted_on, use_llm=use_llm, persist=persist,
                                   evidence_records=evidence_records, use_confirmation_cache=use_confirmation_cache,
                                   llm_fallback=llm_fallback, version=version)
            finally:
                self.provider_resolver = default_resolver

    def _score(self, *, campaign, campaign_candidate, job_description, resume, experience_result, education_result,
               domain_result, acted_on, use_llm, persist, evidence_records, use_confirmation_cache,
               llm_fallback=FALLBACK_NEVER, version=None) -> dict:
        from app.services.campaign.scoring_versions import version_config
        config = version_config(version) if version is not None else self.load_config()
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
                required=getattr(row, "required", False) is True,
            ))

        evidence_rows = evidence_records if evidence_records is not None else self.detection_repository.list_evidence(resume.id)
        evidence_source = "given" if evidence_records is not None else "stored"
        # The code's version plus a fingerprint of the rule data (aliases, scan terms, cue words,
        # pattern rules, certification mappings, verb lists): any rule change makes stored evidence stale.
        current_version = getattr(tools, "version", DETECTION_VERSION)
        stale = evidence_records is None and any(
            getattr(r, "detection_version", current_version) != current_version for r in evidence_rows)
        if (not evidence_rows or stale) and evidence_records is None:
            # None stored yet, or stored by other detection rules: detected again from the stored resume.
            records, _ = detect_stored_resume(self.evidence_service, resume, persist=persist)
            evidence_rows = self.detection_repository.list_evidence(resume.id) if persist else records
            evidence_source = ("re-detected (stored evidence predates the current detection rules)" if stale
                               else "detected now from the stored resume")
        evidence = [
            EvidenceInput(
                canonical_skill_id=r.canonical_skill_id, sense=r.sense, sources=tuple(r.sources or ()),
                best_source=r.best_source, match_tier=r.match_tier, recognition=float(r.recognition),
                evidence_text=((r.evidence or [{}])[0].get("text") or "")[:240],
            )
            for r in evidence_rows
        ]
        resume_text_hash = next((getattr(r, "text_hash", None) for r in evidence_rows if getattr(r, "text_hash", None)), None)

        texts = (self.db.query(SkillOntology.id, SkillOntology.description, SkillOntology.not_description)
                 .filter(SkillOntology.id.in_([r.canonical_skill_id for r in rows])).all()) if rows else []
        specs = jd_core_skill_specs(job_description, rows, names, tools.resolver,
                                    skill_descriptions={i: d for i, d, _ in texts},
                                    skill_not_descriptions={i: n for i, _, n in texts})
        # Phase 3 Step 7: the JD profile template decides which resume sections count as evidence.
        evidence_sections = jd_evidence_sections(job_description)
        auto_sections = jd_auto_confirm_sections(job_description)
        job = job_context(job_description, [getattr(s, "name", None) for s in specs])
        # The candidate's name never reaches the voters (confirmation and paraphrase rounds).
        name_mask = NameMask.for_resume(resume, catalog_terms=tools.index.entries.keys())
        confirmation = {"ran": False, "llm_calls": 0}
        confirmations: dict[str, dict] = {}
        special_outcome = None
        no_llm_now = llm_fallback == FALLBACK_ALWAYS
        if specs and use_llm and self.provider_resolver is not None and not no_llm_now:
            try:
                outcome = CoreSkillConfirmationService(self.provider_resolver(), self.detection_repository).confirm(
                    resume_id=resume.id, jd_id=job_description.id, core_skills=specs,
                    evidence_rows=evidence_rows, text_hash=resume_text_hash or "", persist=persist,
                    budget=self._budget if persist else None, use_cache=use_confirmation_cache,
                    auto_sections=auto_sections, rules=tools.rules, job=job,
                    cap_listed_only_skills=not jd_listed_only_can_confirm(job_description), name_mask=name_mask,
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
        base_evidence = list(evidence)
        if (specs and use_llm and self.provider_resolver is not None and special_outcome is None
                and not no_llm_now and paraphrase_config.get("enabled")):
            try:
                with llm_call_source("paraphrase_confirmation"):
                    paraphrase, extra_evidence = self._paraphrase_round(
                        resume=resume, job_description=job_description, specs=specs, confirmations=confirmations,
                        evidence_rows=evidence_rows, tools=tools, config=paraphrase_config,
                        text_hash=resume_text_hash or "", persist=persist, use_cache=use_confirmation_cache,
                        name_mask=name_mask,
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

        fallback_reason = NO_LLM_ON_REQUEST if no_llm_now else None
        if special_outcome is not None and (
                (special_outcome[0] == OUTCOME_NOT_RUN and llm_fallback in (FALLBACK_ON_CAP, FALLBACK_ON_UNAVAILABLE))
                or (special_outcome[0] == OUTCOME_PENDING and llm_fallback == FALLBACK_ON_UNAVAILABLE)):
            fallback_reason, special_outcome = special_outcome[1], None
        if fallback_reason is not None:
            # The scan, auto-confirm and rules only: no vote, no paraphrase round. The calls the
            # failed attempt made still count (they reached the provider).
            confirmation = {"ran": False, "fallback": True, "llm_calls": int(confirmation.get("llm_calls") or 0),
                            "attempt": {k: v for k, v in confirmation.items() if k != "results"},
                            "results": None}
            confirmations = self._no_llm_confirmations(specs, evidence_rows, resume_text_hash, auto_sections, tools,
                                                       job, job_description, name_mask)
            confirmation["results"] = list(confirmations.values())
            paraphrase = {"ran": False, "llm_calls": 0, "skipped": "no-LLM fallback"}
            evidence = base_evidence

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
            "detection_version": current_version,
            "confirmation_prompt_version": PROMPT_VERSION,
        }
        config_version_hash = config_version(snapshot)

        hierarchy = load_mandatory_skill_hierarchy(
            SkillOntologyRepository(self.db), [s.canonical_skill_id for s in jd_skills if s.mandatory])

        breakdown = score_v2(ScoringInputs(
            jd_skills=jd_skills, evidence=evidence, confirmations=confirmations, hierarchy=hierarchy,
            experience_result=experience_result, education_result=education_result, domain_result=domain_result,
            deterministic_threshold=float(campaign.deterministic_threshold),
            required_skill_coverage_threshold=float(campaign.required_skill_coverage_threshold),
            allowed_missing_core=allowed,
            score_threshold_rejects=snapshot["score_threshold_rejects_config"],
            extra={"scoring_config_version": config_version_hash, "confirmation": confirmation,
                   "scoring_version_id": str(version.id) if version is not None else None,
                   "scoring_version_label": version.label if version is not None else None,
                   # the functional (domain capability) check's detail, for the Deterministic tab
                   "domain_capability_validation": domain_result,
                   "paraphrase": paraphrase, "evidence_source": evidence_source, "acted_on": acted_on,
                   "profile_template": {"key": getattr(jd_profile(job_description), "key", None),
                                        "evidence_sections": sorted(evidence_sections),
                                        "auto_confirm_sections": sorted(auto_sections),
                                        "listed_only_can_confirm": jd_listed_only_can_confirm(job_description),
                                        "paraphrase_sections": paraphrase_config.get("sections")},
                   "job_context": {"title": job.title, "role_domain": job.role_domain}},
            provisional_skills=self.provisional_inputs(job_description, resume, persist=persist),
        ), config)
        if special_outcome is not None:
            apply_unavailable_outcome(breakdown, *special_outcome)
        if fallback_reason is not None:
            apply_no_llm_outcome(breakdown, experience_result, education_result, fallback_reason)
        else:
            breakdown.update(scoring_mode=SCORING_MODE_LLM, awaiting_full_scoring=False)

        if persist:
            ScoringResultRepository(self.db).add(CandidateScoringResult(
                campaign_candidate_id=campaign_candidate.id, scoring_version=SCORING_VERSION,
                score=breakdown["deterministic_score"], passed=breakdown["deterministic_passed"],
                outcome=breakdown["outcome"], breakdown=breakdown, scoring_config_version=config_version_hash,
                config_snapshot=snapshot, acted_on=acted_on, scoring_mode=breakdown["scoring_mode"],
                awaiting_full_scoring=bool(breakdown.get("awaiting_full_scoring")),
                scoring_version_id=version.id if version is not None else None,
            ))
        return breakdown

    def _no_llm_confirmations(self, specs, evidence_rows, text_hash, auto_sections, tools, job, job_description,
                              name_mask) -> dict[str, dict]:
        """
        Core-skill results without a single LLM call: what auto-confirm settles is CONFIRMED;
        every other core skill - found but needing a vote, or not found at all (the paraphrase
        round might have found it) - is UNCERTAIN, awaiting full scoring.
        """
        if not specs:
            return {}
        outcome = CoreSkillConfirmationService(None, None).confirm(
            resume_id=None, jd_id=job_description.id, core_skills=specs, evidence_rows=evidence_rows,
            text_hash=text_hash or "", persist=False, budget=lambda _calls: False, use_cache=False,
            auto_sections=auto_sections, rules=tools.rules, job=job,
            cap_listed_only_skills=not jd_listed_only_can_confirm(job_description), name_mask=name_mask,
        )
        results = {}
        for result in outcome.results:
            if result.get("status") != STATUS_CONFIRMED:
                result = {**result, "status": STATUS_UNCERTAIN, "reason": REASON_AWAITING_FULL_SCORING,
                          "confirmed": False}
            results[result["canonical_skill_id"]] = result
        return results


ROLE_DOMAIN_LABELS = {"SOFTWARE": "Software", "IT_OPERATIONS": "IT operations", "DATA": "Data",
                      "BUSINESS": "Business", "OTHER": "Other"}


def job_context(job_description, core_skill_names: list[str]) -> JobContext:
    """The JD's title and role domain for the voters - as calibrated, else the suggested one (unset JDs only)."""
    title = getattr(job_description, "title", None)
    title = title if isinstance(title, str) and title.strip() else None
    names = [n for n in core_skill_names if isinstance(n, str)]
    domain = jd_role_domain(job_description) or suggest_role_domain(title, names).domain
    return JobContext(title=title, role_domain=ROLE_DOMAIN_LABELS.get(domain, domain) if domain else None)


def _certification_only(evidence_rows: list, spec) -> bool:
    """Every evidence item for this core skill (or its JD aliases) comes from a certification."""
    items = [item for row in evidence_rows if getattr(row, "canonical_skill_id", None) in spec.skill_ids
             for item in (getattr(row, "evidence", None) or [])]
    return bool(items) and all(str(item.get("origin") or "").startswith(CERTIFICATION_ORIGIN) for item in items)


def _paraphrase_targets_for(specs, confirmations: dict, skills_by_id: dict,
                            evidence_rows: list | None = None) -> list[ParaphraseTarget]:
    targets = []
    for spec in specs:
        status = (confirmations.get(str(spec.canonical_skill_id)) or {}).get("status", STATUS_NOT_FOUND)
        # A "no" to certification evidence alone ("ITIL 4 Foundation") says
        # nothing about the work history: the skill stays open for paraphrase.
        reopened = status == STATUS_REJECTED and _certification_only(evidence_rows or [], spec)
        if status not in (STATUS_NOT_FOUND, STATUS_UNCERTAIN) and not reopened:
            continue                       # CONFIRMED / REJECTED / SENSE_MISMATCH (decided) / UNCONFIRMED
        skill = skills_by_id.get(spec.canonical_skill_id)
        targets.append(ParaphraseTarget(
            canonical_skill_id=spec.canonical_skill_id, name=spec.name,
            aliases=tuple(getattr(skill, "aliases", None) or ()), description=getattr(skill, "description", None),
            sense=spec.sense, sense_basis=spec.sense_basis, sense_description=spec.sense_description,
            requirement_text=getattr(spec, "requirement_text", None),
        ))
    return targets


def v2_rejection_reason(breakdown: dict) -> str:
    """Plain-language reason when v2 rejects (used only when v2 decides)."""
    parts = []
    for check in breakdown.get("checks", []):
        if not check["passed"]:
            parts.append(f"{check['check']}: {check.get('detail') or 'not met'}")
    if breakdown.get("core_rejected_by_llm"):
        parts.append("Not shown on the resume: " + ", ".join(breakdown["core_rejected_by_llm"]))
    return " | ".join(parts)[:500] or "Did not meet the deterministic requirements."
