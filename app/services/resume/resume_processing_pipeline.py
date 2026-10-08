import dataclasses
import hashlib
import json
import logging
from uuid import UUID

from app.models.async_tasks import ProcessingStage
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.repositories.resume_repository import ResumeRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.ai.resume_extraction_response import ResumeExtractionGenerationSchema, ResumeExtractionResponse
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.extractions.llm_extraction_service import LLMExtractionService
from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_redaction_service import PIIRedactionService
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.resume.resume_processing_context import ResumeProcessingContext
from app.services.resume.resume_service import ResumeService
from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
from app.services.skills.compound_skill_splitter import split_compound_skill
from app.services.llm.structured_call import call_info
from app.services.skills.resume_skill_scanner import DETECTION_VERSION
from app.services.skills.skill_evidence_service import LLMSkillMatch, ParseInfo, job_titles_of, text_hash
from app.services.skills.text_evidence import evidence_found_in  # noqa: F401 - re-exported
from app.services.skills.skill_normalization_service import SkillMatchTier, SkillNormalizationService
from app.core.storage_service import StorageService

logger = logging.getLogger(__name__)


# Split parts and inferred skills are the system's own reading of the resume,
# so they only count on a certain match - a fuzzy/semantic near-miss (e.g. a
# fragment "P Governance" fuzzing to "AI Governance") must never add a skill.
_SUPPLEMENTARY_MATCH_TIERS = frozenset({
    SkillMatchTier.EXACT, SkillMatchTier.ALIAS, SkillMatchTier.CASE_INSENSITIVE, SkillMatchTier.RULE_BASED,
})


def parse_cache_key(text: str | None, prompt_hash: str, provider: str | None, model: str | None) -> str:
    """Resume text + prompt version + provider/model + detection version."""
    payload = json.dumps([text_hash(text), prompt_hash, provider or "", model or "", DETECTION_VERSION])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResumeProcessingPipeline:
    """
    Orchestrates the Resume document-processing pipeline: Text Extraction ->
    Text Cleaning -> AI Extraction -> JSON Validation -> Skill Normalization
    -> Persistence. Mirrors JDProcessingPipeline's stage loop and
    StageExecutionService usage, with one deliberate deviation: stages are
    run WITHOUT `context=`/`checkpoint_repo=` args.

    M08-E01: embedding generation is no longer a stage of this pipeline -
    it's now EMBED_RESUME, a separate, decoupled Celery task
    (app/tasks/embedding_tasks.py) enqueued after this pipeline succeeds,
    mirroring exactly how DETERMINISTIC_SCORE is already enqueued after
    resume processing completes. This avoids a resume ever getting two
    embedding rows (one from this pipeline, one from EMBED_RESUME) and
    gives embedding generation its own dedup/anonymisation-verification/
    task-log pipeline independent of parsing.

    StageExecutionService.run_stage's failure branch calls
    app.services.jd.context_serializer.to_dict(context) unconditionally
    (that import is hardcoded, not dispatched by document_type) — passing a
    ResumeProcessingContext into it would crash on the first failed stage,
    since that serializer reads JD-only attributes (title, extraction,
    jd_id, ...). Omitting context/checkpoint_repo keeps per-stage
    success/failure logging (StageExecutionService itself) working exactly
    like JD's, at the cost of mid-run checkpoint resume: a retried Celery
    task for a resume re-runs every stage from TEXT_EXTRACTION rather than
    resuming from the failed stage. Fixing this properly means making
    StageExecutionService dispatch its serializer by document_type, which
    is out of scope here (JD-facing shared file, not the one permitted
    change).

    Concrete and Resume-specific by design, same reasoning as
    JDProcessingPipeline's own docstring.
    """

    RESUME_STORAGE_BUCKET = ResumeService.RESUME_STORAGE_BUCKET

    def __init__(
        self,
        *,
        preprocessing_service: PreprocessingService,
        extraction_service: LLMExtractionService,
        storage_service: StorageService,
        skill_normalization_service: SkillNormalizationService,
        resume_service: ResumeService,
        resume_repository: ResumeRepository,
        skill_repository: SkillRepository,
        stage_tracker: StageExecutionService,
        pii_detection_service: PIIDetectionService,
        pii_redaction_service: PIIRedactionService,
        prompt_template_repository: PromptTemplateRepository,
        skill_evidence_service=None,
        detection_repository=None,
    ):
        self.preprocessing_service = preprocessing_service
        self.extraction_service = extraction_service
        self.storage_service = storage_service
        self.skill_normalization_service = skill_normalization_service
        self.resume_service = resume_service
        self.resume_repository = resume_repository
        self.skill_repository = skill_repository
        self.stage_tracker = stage_tracker
        self.pii_detection_service = pii_detection_service
        self.pii_redaction_service = pii_redaction_service
        self.prompt_template_repository = prompt_template_repository
        # Scoring v2 (Phase 1) - both optional; without them the pipeline
        # behaves exactly as before (no parse cache, no evidence rows).
        self.skill_evidence_service = skill_evidence_service
        self.detection_repository = detection_repository

    def run(
        self,
        *,
        task_id: str,
        resume_id: UUID,
        candidate_id: UUID,
        file_path: str,
        source_format: ResumeSourceFormat,
        prompt_template_id: UUID,
        attempt_number: int = 1,
        initial_context: ResumeProcessingContext | None = None,
        parse_cache_enabled: bool = False,
        bypass_parse_cache: bool = False,
        reparse: bool = False,
    ) -> UUID:
        """
        initial_context lets a caller that already ran some of these stages
        itself (bulk upload, which must extract text and run AI extraction
        before Candidate/Resume — and therefore this context — can exist)
        hand in a context with those stages' outputs already populated.

        A stage whose expected output is already present on the context is
        skipped entirely — not re-run, and no skip_stage() call either. The
        caller that populated the context (bulk's own pre-identity
        stage_tracker.run_stage() calls) already wrote the real SUCCESS
        document_processing_stage_executions row for it in this same
        attempt; calling skip_stage() here would try to write a second
        record for the exact same (task_id, stage, attempt_number) and
        overwrite that real row with a SKIPPED one instead. This differs
        from JDProcessingPipeline's _should_skip_stage/skip_stage pattern,
        which exists for a genuinely different case (a checkpoint-resumed
        retry where the stage truly did not run in this attempt) — bulk's
        case is "already ran, just not through this method," not "never
        ran." An individual-upload call never passes initial_context, so
        every stage always runs exactly as before this change.
        """
        context = initial_context or ResumeProcessingContext(
            task_id=task_id,
            file_path=file_path,
            source_format=source_format,
        )
        context.resume_id = resume_id
        context.candidate_id = candidate_id
        context.prompt_template_id = prompt_template_id
        context.attempt_number = attempt_number
        context.parse_cache_enabled = parse_cache_enabled
        context.bypass_parse_cache = bypass_parse_cache or reparse
        context.reparse = reparse

        for stage, output_attr, fn in (
            (ProcessingStage.TEXT_EXTRACTION, "raw_text", lambda: self._run_text_extraction(context)),
            (ProcessingStage.TEXT_CLEANING, "cleaned_text", lambda: self._run_text_cleaning(context)),
            (ProcessingStage.PII_DETECTION, "pii_findings", lambda: self._run_pii_detection(context)),
            (ProcessingStage.PII_REDACTION, "redacted_text", lambda: self._run_pii_redaction(context)),
            (ProcessingStage.AI_EXTRACTION, "raw_extraction", lambda: self._run_ai_extraction(context)),
            (ProcessingStage.JSON_VALIDATION, "validated_extraction", lambda: self._run_json_validation(context)),
            (ProcessingStage.SKILL_NORMALIZATION, "skill_match_results", lambda: self._run_skill_normalization(context)),
            (ProcessingStage.PERSISTENCE, None, lambda: self._run_persistence(context)),
        ):
            logger.warning("=== STAGE STARTING: %s === resume_id=%s", stage.value, context.resume_id)
            if output_attr is not None and getattr(context, output_attr) is not None:
                continue
            self.stage_tracker.run_stage(
                context.task_id,
                context.document_type,
                stage,
                fn,
                attempt_number=attempt_number,
            )
            logger.warning("=== STAGE COMPLETED: %s === resume_id=%s", stage.value, context.resume_id)

        self.stage_tracker.link_document_id(
            context.task_id, context.resume_id, document_type=context.document_type,
        )

        self._run_skill_detection_v2(context)

        logger.warning("=== ResumeProcessingPipeline.run() RETURNING === resume_id=%s", context.resume_id)
        return context.resume_id

    def _run_text_extraction(self, context: ResumeProcessingContext) -> None:
        file_content = self.storage_service.download_file(
            bucket_name=self.RESUME_STORAGE_BUCKET,
            file_path=context.file_path,
        )
        context.raw_text = ResumeTextExtractionService.extract(file_content, context.source_format)
        if context.source_format == ResumeSourceFormat.PDF:
            context.page_count = TextExtractionService.get_pdf_page_count(file_content)

    def _run_text_cleaning(self, context: ResumeProcessingContext) -> None:
        context.cleaned_text = self.preprocessing_service.normalize(context.raw_text)

    def _run_pii_detection(self, context: ResumeProcessingContext) -> None:
        context.pii_findings = self.pii_detection_service.detect(context.cleaned_text)

    def _run_pii_redaction(self, context: ResumeProcessingContext) -> None:
        context.redacted_text = self.pii_redaction_service.redact(context.cleaned_text, context.pii_findings)

    def _run_ai_extraction(self, context: ResumeProcessingContext) -> None:
        prompt_template = self.prompt_template_repository.get_by_id(context.prompt_template_id)
        if prompt_template is None:
            raise ValueError(f"Prompt template '{context.prompt_template_id}' no longer exists.")
        provider = self.extraction_service.provider
        cache_key = None
        if self.detection_repository is not None:
            cache_key = parse_cache_key(context.redacted_text, prompt_template.content_hash,
                                        getattr(provider, "provider_name", None), getattr(provider, "model", None))
            if context.parse_cache_enabled and not context.bypass_parse_cache:
                cached = self._cache_lookup(cache_key)
                if cached is not None:
                    context.raw_extraction = cached.raw_extraction
                    context.parse_call = {"provider": cached.provider, "model": cached.model,
                                          "used_fallback": False, "cache_hit": True}
                    return

        context.raw_extraction = self.extraction_service.extract_raw(
            context.redacted_text,
            prompt=prompt_template.template_text,
            response_schema=ResumeExtractionGenerationSchema,
        )
        call = call_info(provider)
        context.parse_call = {**call.as_dict(), "cache_hit": False}
        if cache_key is not None:
            self._cache_store(context, prompt_template.content_hash, call)

    def _cache_lookup(self, cache_key: str):
        try:
            entry = self.detection_repository.get_parse_cache(cache_key)
            if entry is not None:
                self.detection_repository.mark_parse_cache_hit(entry)
            return entry
        except Exception:
            logger.exception("Parse cache lookup failed - parsing normally.")
            return None

    def _cache_store(self, context: ResumeProcessingContext, prompt_hash: str, call) -> None:
        """Stored under the model that answered - a backup model's answer never serves as the main model's."""
        try:
            self.detection_repository.put_parse_cache(
                cache_key=parse_cache_key(context.redacted_text, prompt_hash, call.provider, call.model),
                text_hash=text_hash(context.redacted_text), prompt_hash=prompt_hash,
                provider=call.provider, model=call.model, detection_version=DETECTION_VERSION,
                raw_extraction=context.raw_extraction,
            )
        except Exception:
            logger.exception("Parse cache store failed - parse result unaffected.")

    def _run_json_validation(self, context: ResumeProcessingContext) -> None:
        extraction = ResumeExtractionResponse.model_validate(context.raw_extraction)
        # Inferred skills must quote the resume - drop any whose evidence
        # isn't in the text the model was given, before it can be stored
        # in parsed_json or counted in matching.
        verified = [
            item for item in extraction.inferred_skills
            if evidence_found_in(item.evidence, context.redacted_text)
        ]
        if len(verified) != len(extraction.inferred_skills):
            logger.warning(
                "Dropped %s inferred skill(s) whose evidence isn't in the resume | resume_id=%s",
                len(extraction.inferred_skills) - len(verified), getattr(context, "resume_id", None),
            )
            extraction.inferred_skills = verified
        context.validated_extraction = extraction

    def _run_skill_normalization(self, context: ResumeProcessingContext) -> None:
        extraction = context.validated_extraction
        primary = self.skill_normalization_service.normalize_skills(
            required_skills=extraction.skills, preferred_skills=[],
        )
        context.skill_match_results = primary + self._supplementary_matches(extraction, primary)

    def _supplementary_matches(self, extraction: ResumeExtractionResponse, primary: list) -> list:
        """
        Extra matches from (a) the parts of compound skills ("IT Project &
        Program Management" -> Program Management) and (b) evidence-backed
        inferred skills (PMP -> Program Management). Each is kept only when
        it resolves - on an exact/alias/case/rule match, never fuzzy or
        semantic - to a known catalog skill the resume doesn't already
        hold - an unmatched part/inference is dropped rather than added to
        the unknown-skill review pool, so neither can create noise. The
        stored raw text records where the skill came from.
        """
        candidates: list[tuple[str, str]] = []
        for phrase in extraction.skills:
            for part in split_compound_skill(phrase):
                candidates.append((part, f'{part} (from "{phrase}")'))
        for inferred in extraction.inferred_skills:
            candidates.append((inferred.skill, f"{inferred.skill} (inferred: {inferred.evidence})"))
        if not candidates:
            return []

        results = self.skill_normalization_service.normalize_skills(
            required_skills=[name for name, _ in candidates], preferred_skills=[],
        )
        held = {r.canonical_skill_id for r in primary if r.canonical_skill_id}
        extra = []
        for (_, raw_text), result in zip(candidates, results):
            if (
                result.canonical_skill_id
                and result.match_tier in _SUPPLEMENTARY_MATCH_TIERS
                and result.canonical_skill_id not in held
            ):
                held.add(result.canonical_skill_id)
                extra.append(dataclasses.replace(result, raw_text=raw_text))
        return extra

    def _run_skill_detection_v2(self, context: ResumeProcessingContext) -> None:
        """
        Scoring v2 evidence (scan + LLM union) for this resume. Runs after
        the v1 parse is committed and never affects it: any failure is
        logged and the resume stays parsed exactly as v1 sees it.
        """
        if self.skill_evidence_service is None or context.redacted_text is None:
            return
        try:
            call = context.parse_call or {}
            self.skill_evidence_service.detect(
                context.resume_id,
                text=context.redacted_text,
                llm_matches=[
                    LLMSkillMatch(m.canonical_skill_id, m.raw_text, m.match_tier.value)
                    for m in context.skill_match_results or [] if m.canonical_skill_id
                ],
                parse=ParseInfo(call.get("provider"), call.get("model"), bool(call.get("used_fallback"))),
                job_titles=job_titles_of(getattr(context, "validated_extraction", None)),
            )
            self.resume_repository.commit()
        except Exception:
            logger.exception("Scoring v2 skill detection failed | resume_id=%s", context.resume_id)
            try:
                self.resume_repository.rollback()
            except Exception:
                logger.exception("Rollback after v2 detection failure failed | resume_id=%s", context.resume_id)

    def _run_persistence(self, context: ResumeProcessingContext) -> None:
        resume = self.resume_repository.get_by_id(context.resume_id)
        if resume is None:
            raise ValueError(f"Resume with ID {context.resume_id} not found.")

        self.resume_service.persist_processed_resume(
            resume=resume,
            extraction=context.validated_extraction,
            skill_repository=self.skill_repository,
            skill_matches=context.skill_match_results,
            attempt_number=context.attempt_number,
            page_count=context.page_count,
            replace_existing_skills=getattr(context, "reparse", False),
        )
