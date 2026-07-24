# Bulk Upload Pipeline Refactor — Architectural Migration Analysis

**Status:** Analysis only. No code has been modified to produce this document.
**Prepared:** 2026-07-21
**Scope:** M05 Resume Intake — unifying Bulk ZIP Upload's per-file processing with Individual Resume Upload's `ResumeProcessingPipeline`.

**Philosophy driving this analysis:** *"Bulk Upload should only orchestrate bulk-specific work. Resume processing should have a single implementation."*

---

## SECTION 1 — CURRENT ARCHITECTURE ANALYSIS

### 1.1 Individual Upload

**Entry point:** `POST /resumes` — `app/api/routes/resume_routes.py` → `ResumeIntakeService.upload_resume()` (`app/services/resume/resume_intake_service.py`)

| Step | Entry point | Service | Repository | DB tables | Celery task | Models used | Output |
|---|---|---|---|---|---|---|---|
| 1. Campaign pre-check | `ResumeIntakeService._precheck_campaign_eligibility` | — | `CampaignRepository` | `hiring_campaigns` | — | `HiringCampaign` | Fails fast (404/409) or returns campaign |
| 2. File validation | `ResumeUploadService.upload` | `FileValidationService` | — | — | — | — | `validation_result` (format, size OK) |
| 3. Storage | `ResumeUploadService.upload` | `StorageService` | — | Supabase bucket `airs_resumes` | — | — | `object_path` |
| 4. Candidate identity | `ResumeUploadService.upload` | `CandidateService.get_or_create` (→ `EncryptionService`, `ConsentService`) | `CandidateRepository` | `candidates`, `candidate_consent` | — | `Candidate` | Existing or new `Candidate` (encrypted PII, `email_hash` dedup) |
| 5. Resume row | `ResumeUploadService.upload` | — | `ResumeRepository` | `resumes` | — | `Resume` (`parse_status=PENDING`) | `resume.id` |
| 6. Campaign-candidate | `ResumeIntakeService.upload_resume` | `CampaignCandidateService.create_campaign_candidate` | `CampaignCandidateRepository` | `campaign_candidates`, `campaign_candidate_stage_history` | — | `CampaignCandidate` | Race-safe insert, `pipeline_stage=UPLOADED` |
| 7. Audit + enqueue | `ResumeIntakeService.upload_resume` | `AuditService` | `AuditRepository` | `audit_log` | `process_resume_document.apply_async` | — | `task_id` returned to client; **no `celery_task_log` row yet** |
| 8. Text extraction | `ResumeProcessingPipeline._run_text_extraction` | `ResumeTextExtractionService` (thin wrapper over `TextExtractionService.extract_pdf_text`/`extract_docx_text`) | — | storage read | `resume.process_document` | — | `context.raw_text` |
| 9. Text cleaning | `ResumeProcessingPipeline._run_text_cleaning` | `PreprocessingService` | — | — | (same task) | — | `context.cleaned_text` |
| 10. AI extraction | `ResumeProcessingPipeline._run_ai_extraction` | `GeminiExtractionService.extract_raw` (JD's shared class, prompt/schema overridden to `RESUME_SYSTEM_PROMPT`/`ResumeExtractionGenerationSchema`) | — | Gemini API | (same task) | — | `context.raw_extraction` (dict) |
| 11. JSON validation | `ResumeProcessingPipeline._run_json_validation` | — | — | — | (same task) | `ResumeExtractionResponse` | `context.validated_extraction` |
| 12. Skill normalization | `ResumeProcessingPipeline._run_skill_normalization` | `SkillNormalizationService` | `SkillRepository` (read-only here) | `skill_ontology` (read) | (same task) | `SkillMatchResult` | `context.skill_match_results` |
| 13. Embedding generation | `ResumeProcessingPipeline._run_embedding_generation` | `EmbeddingService`, `HashService`, `resume_embedding_text_builder` | — | — | (same task) | — | `context.embedding`, `context.input_text_hash` |
| 14. Persistence | `ResumeProcessingPipeline._run_persistence` | `ResumeService.persist_processed_resume` | `ResumeRepository`, `SkillRepository` | `resumes` (update), `candidate_skills`, `resume_embeddings`, `audit_log` | (same task) | `Resume`, `CandidateSkill`, `ResumeEmbedding` | `parse_status=PARSED`, all downstream data written atomically |

**Stage tracking:** Steps 8–14 each run through `StageExecutionService.run_stage()`, writing a `document_processing_stage_executions` row per stage (`RUNNING` → `SUCCESS`/`FAILED`, with duration). `stage_tracker.link_document_id()` retroactively links those rows to `resume_id` once known.

**Retry:** Any stage exception → `StageExecutionError(stage, exc)` → `RetryDriver.handle_failure()` — classifies via `error_classifier.classify()`, checks `STAGE_POLICIES` (only `AI_EXTRACTION` has a non-default policy: 5 attempts), either calls `self.retry(countdown=...)` (real Celery re-queue) or writes to `dead_letter_queue` and returns `False`.

**Task logging:** `celery_task_log` row created/marked-running at task start (not before), `mark_success`/`mark_failure`/`mark_retry` transitions via `CeleryTaskLogService`.

### 1.2 Bulk Upload

**Entry point:** `POST /bulk-uploads` — `app/api/routes/bulk_upload_routes.py` → `BulkUploadService.upload_zip()` (`app/services/bulk_upload/bulk_upload_service.py`)

| Step | Entry point | Service | Repository | DB tables | Celery task | Models used | Output |
|---|---|---|---|---|---|---|---|
| 1. Campaign pre-check | `BulkUploadService._precheck_campaign_eligibility` | — | `CampaignRepository` | `hiring_campaigns` | — | `HiringCampaign` | Same shape as individual's, duplicated implementation |
| 2. ZIP validation | `BulkUploadService.upload_zip` | `ZipValidationService` | `ConfigRepository` | `platform_config` (read) | — | — | Size/format OK |
| 3. ZIP storage + job | `BulkUploadService.upload_zip` | `StorageService` | `BulkUploadJobRepository` | Supabase bucket `airs_resumes`, `bulk_upload_jobs` | `bulk_upload.extract_zip` (`extract_bulk_upload_zip`) | `BulkUploadJob` | `task_id`, job at `PENDING` |
| 4. Extraction | `extract_bulk_upload_zip` | `ZipValidationService.validate_file_count`, `StorageService` | `BulkUploadJobFileRepository`, `BulkUploadJobRepository` | `bulk_upload_job_files`, `bulk_upload_jobs` | (same task) | `BulkUploadJobFile` | One `QUEUED` file row + one staged storage object per real ZIP entry |
| 5. Per-file enqueue | `extract_bulk_upload_zip` | — | — | — | `bulk_upload.parse_file` × N (`parse_bulk_upload_file`) | — | One Celery task per file |
| 6. Storage download | `parse_bulk_upload_file` (`_run_stage`, `STORAGE`) | `StorageService` | — | — | (same task) | — | `file_bytes` |
| 7. File validation | `parse_bulk_upload_file` (unwrapped) | `FileValidationService` | — | — | (same task) | — | `validation_result` — deterministic rejection, not retried |
| 8. Text extraction | `parse_bulk_upload_file` (`_run_stage`, `TEXT_EXTRACTION`) | `TextExtractionService.extract_for_resume` (same underlying `extract_pdf_text`/`extract_docx_text` as individual) | — | — | (same task) | — | `text` |
| 9. Text cleaning | `parse_bulk_upload_file` (`_run_stage`, `TEXT_CLEANING`) | `PreprocessingService` | — | — | (same task) | — | `cleaned_text` |
| 10. AI extraction | `parse_bulk_upload_file` (`_run_stage`, `AI_EXTRACTION`) | `GeminiResumeExtractionService.extract` (bulk's **own** class, own `SYSTEM_PROMPT`, freeform JSON mode) | — | Gemini API | (same task) | `ResumeExtractionResponse` | `extracted` |
| 11. Identity check | `parse_bulk_upload_file` (unwrapped) | — | — | — | (same task) | — | Deterministic rejection if no `full_name`/`email` |
| 12. Candidate identity | `parse_bulk_upload_file` (unwrapped) | `CandidateService.get_or_create` | `CandidateRepository` | `candidates`, `candidate_consent` | (same task) | `Candidate` | Same dedup logic as individual, **no per-stage tracking** |
| 13. Resume + parse-attempt | `parse_bulk_upload_file` (unwrapped) | — | `ResumeRepository` | `resumes`, `resume_parse_attempts` | (same task) | `Resume` (`parse_status=PENDING`), `ResumeParseAttempt` | `resume.id`; **`resume_parse_attempts` is bulk-only — individual never writes it** |
| 14. Skill normalization | `parse_bulk_upload_file` (`_run_stage`, `SKILL_NORMALIZATION`) | `SkillNormalizationService` (same class) | `SkillRepository` | `skill_ontology` (read) | (same task) | `SkillMatchResult` | `skill_match_results` |
| 15. Embedding generation | `parse_bulk_upload_file` (`_run_stage`, `EMBEDDING_GENERATION`) | `EmbeddingService`, `HashService`, `resume_embedding_text_builder` (same classes) | — | — | (same task) | — | `embedding`, `input_text_hash` |
| 16. Persistence | `parse_bulk_upload_file` (`_run_stage`, `PERSISTENCE`) | `ResumeService.persist_processed_resume` (same method) | `ResumeRepository`, `SkillRepository` | `resumes`, `candidate_skills`, `resume_embeddings`, `audit_log` | (same task) | Same models as individual | Same write shape as individual |
| 17. Campaign-candidate | `parse_bulk_upload_file` (unwrapped) | `CampaignCandidateService.create_campaign_candidate` (same method) | `CampaignCandidateRepository` | `campaign_candidates` | (same task) | `CampaignCandidate` | Same insert as individual |
| 18. Job finalization | `parse_bulk_upload_file` | — | `BulkUploadJobFileRepository`, `BulkUploadJobRepository` | `bulk_upload_job_files`, `bulk_upload_jobs` | (same task) | — | Per-file status + job-level counters/terminal status |

**Stage tracking:** Steps 6, 8–10, 14–16 run through the file's own module-level `_run_stage()` helper — checkpoint-only (writes `document_processing_checkpoints` on failure), **no `document_processing_stage_executions` rows at all**. Steps 7, 11, 12, 13, 17 are not stage-tracked in any form.

**Retry:** Same `RetryDriver`/`error_classifier`/`STAGE_POLICIES` machinery, instantiated with `task_type=BULK_RESUME_PARSE` instead of `RESUME_DOCUMENT_PROCESSING` — otherwise identical classification/backoff logic.

**Task logging:** Same `CeleryTaskLogService`, `bulk_upload_job_id` populated on the row (individual's row never has this).

---

## SECTION 2 — PIPELINE COMPARISON

| Stage | Individual implementation | Bulk implementation | Same? | Reusable? | Needs refactor? | Expected target |
|---|---|---|---|---|---|---|
| Validation | `FileValidationService.validate` (unwrapped, no stage tracking) | `FileValidationService.validate` (unwrapped, no stage tracking) | **Yes** — identical class/method | Already reused | No | Unchanged; optionally wrap in `VALIDATION` stage (enum value exists, unused by either flow today) |
| Storage (download) | Inline in `_run_text_extraction`, via `StorageService.download_file` | `_run_stage(STORAGE, ...)`, via `StorageService.download_file` | Same underlying service, different wrapping | Already reused | Minor — individual doesn't track `STORAGE` as its own stage, bulk does | Track as its own stage for both, or fold into `TEXT_EXTRACTION` for both — pick one convention |
| Text Extraction | `ResumeTextExtractionService.extract` → `TextExtractionService.extract_pdf_text/extract_docx_text` | `TextExtractionService.extract_for_resume` → same two methods | **Yes**, underlying logic — only the enum-keyed wrapper differs (`ResumeSourceFormat` vs `FileFormat`) | Already reused | Cosmetic only | Keep one wrapper; bulk's identity-discovery ordering (extract text before identity exists) still requires this stage to run before `ResumeProcessingPipeline` can be invoked |
| Cleaning | `PreprocessingService.normalize` | `PreprocessingService.normalize` | **Yes**, identical | Already reused | No | Unchanged |
| AI Extraction | `GeminiExtractionService.extract_raw` (JD's class, overridden prompt/schema, **schema-constrained** generation) | `GeminiResumeExtractionService.extract` (bulk's own class, own prompt, **freeform JSON** generation) | **No** — different class, different prompt, different Gemini generation mode | Partially — schema output is compatible (`ResumeExtractionResponse`) but extraction *quality* differs (see Section 6) | **Yes — real refactor** | Bulk must adopt `RESUME_SYSTEM_PROMPT` + schema-constrained generation; identity fields (`full_name`/`email`/`phone`) must still be requested — a shared extraction path needs both |
| JSON Validation | `ResumeExtractionResponse.model_validate` | Performed *inside* `GeminiResumeExtractionService.extract` itself | Functionally same validation, different call site | Reusable once AI Extraction is unified | Minor | Fold into the same explicit `JSON_VALIDATION` stage bulk currently skips |
| Candidate Creation | `CandidateService.get_or_create` | `CandidateService.get_or_create` | **Yes**, identical method, identical dedup logic | Already reused | No functional change; **not stage-tracked in either flow** | Consider tracking as its own stage in the unified design |
| Resume Creation | `ResumeRepository.create` (via `ResumeUploadService`, before any Celery task exists) | `ResumeRepository.create` (inline in `parse_bulk_upload_file`, after AI extraction) | Same repository method, **different point in the overall flow** — this is the core architectural divergence | Reusable | **Yes** | Bulk's per-file task should create `Resume` at `PENDING` immediately after identity resolution, then hand off — mirroring individual's split between "create the row" and "process it" |
| Skill Normalization | `SkillNormalizationService.normalize_skills` | `SkillNormalizationService.normalize_skills` | **Yes**, identical | Already reused | No | Unchanged |
| Embedding Generation | `EmbeddingService.generate_embedding` + `resume_embedding_text_builder` + `HashService` | Same three components | **Yes**, identical | Already reused | No | Unchanged |
| Persistence | `ResumeService.persist_processed_resume` | Same method | **Yes**, identical | Already reused | No | Unchanged |
| Retry | `RetryDriver` + `error_classifier` + `STAGE_POLICIES`, `task_type=RESUME_DOCUMENT_PROCESSING` | Same classes, `task_type=BULK_RESUME_PARSE` | **Yes**, structurally identical | Already reused | No | Unchanged — `task_type` string naturally differs and should |
| Stage Tracking | `StageExecutionService.run_stage` → `document_processing_stage_executions` | Local `_run_stage()` → `document_processing_checkpoints` only | **No** | `StageExecutionService` is fully reusable (document-type-agnostic by design — see Section 5) | **Yes — the central refactor** | Bulk adopts `StageExecutionService` for every stage from `AI_EXTRACTION` onward |
| Audit | `AuditService.log(...)` at `RESUME_UPLOADED` and (via persistence) `RESUME_PARSED`/`CANDIDATE_SKILL_MATCHED` | Same three audit events, same `AuditService` | **Yes**, identical | Already reused | No | Unchanged |
| Campaign Mapping | `CampaignCandidateService.create_campaign_candidate` | Same method | **Yes**, identical (bulk's call omits `actor_role`) | Already reused | Cosmetic | Pass `actor_role` for parity if available |
| Task Logging | `CeleryTaskLogService`, no `bulk_upload_job_id` | Same service, `bulk_upload_job_id` populated | **Yes**, same service | Already reused | No | Unchanged |

---

## SECTION 3 — CODE DUPLICATION ANALYSIS

| Duplication | Current location(s) | Recommended shared location | Risk | Estimated effort |
|---|---|---|---|---|
| **Gemini extraction service** (two classes, two prompts, two generation modes) | `app/services/extractions/gemini_extraction_service.py` (`GeminiExtractionService`, reused cross-document-type from JD) vs `app/services/extractions/gemini_resume_extraction_service.py` (`GeminiResumeExtractionService`, bulk-only) | A single `ResumeExtractionService` (new or repurposed) that always uses `RESUME_SYSTEM_PROMPT` + schema-constrained generation, and always requests `full_name`/`email`/`phone` (harmless for individual upload, which simply ignores those fields) | **High** — this is a genuine data-quality gap today, not just duplication (see Section 6) | Medium (2–3 days incl. prompt-output regression testing) |
| **Resume-prompt content** | `SYSTEM_PROMPT` vs `RESUME_SYSTEM_PROMPT`, both in `app/prompts/resume_extraction_prompt.py` | Retire `SYSTEM_PROMPT`; `RESUME_SYSTEM_PROMPT` extended to also request `full_name`/`email`/`phone` (fields already exist on `ResumeExtractionResponse`) | Medium — prompt changes can shift extraction behavior for *existing* individual-upload traffic; needs a side-by-side sample-resume regression pass before cutover | Small (prompt edit) + Medium (validation) |
| **Stage execution / progress tracking** | `_run_stage()` (module-level function in `bulk_upload_tasks.py`, checkpoint-only) vs `StageExecutionService.run_stage()` (`app/services/document_processing/stage_execution_service.py`, full DB tracking) | `StageExecutionService` — already document-type-agnostic (`DocumentProcessingRepository` keys everything on `task_id`, not document type) | Low — `DocumentProcessingStageExecution`'s own docstring already states "Document-type-agnostic so a future Resume pipeline reuses it as-is," confirming this was anticipated | Medium (rewiring bulk's per-file task, not the shared service) |
| **Retry driver instantiation** | Duplicated *construction* (not logic) in both `resume_processing_tasks.py` and `bulk_upload_tasks.py` — same repos, same service, different `task_type` string | No change needed — this is intentional, correct duplication (each task type needs its own dead-letter/task-type tag) | None | N/A — not actually a defect |
| **Persistence** | Already fully shared — `ResumeService.persist_processed_resume` is called identically by both flows since yesterday's fix | Already unified | None | Done |
| **Skill normalization / Embedding generation** | Already fully shared — `SkillNormalizationService`, `EmbeddingService`, `resume_embedding_text_builder`, `HashService` all reused verbatim since yesterday's fix | Already unified | None | Done |
| **Text extraction** | `ResumeTextExtractionService.extract` vs `TextExtractionService.extract_for_resume` — **not real duplication**, both delegate to the same `extract_pdf_text`/`extract_docx_text` static methods, just keyed on different enums (`ResumeSourceFormat` vs `FileFormat`) | Optional: collapse to one wrapper keyed on `FileFormat` only, since `ResumeSourceFormat` (PDF/DOCX only) is a strict subset of `FileFormat` | Very low — cosmetic | Small |
| **Campaign pre-check** | `ResumeIntakeService._precheck_campaign_eligibility` vs `BulkUploadService._precheck_campaign_eligibility` — near-verbatim copies (previously flagged in the Release Readiness Review, unrelated to this pipeline but worth noting here since it's the same class of duplication) | A shared `CampaignEligibilityChecker` or a method on `CampaignService` | Low | Small |
| **`resume_parse_attempts` writing** | Only `bulk_upload_tasks.py` calls `ResumeRepository.record_parse_attempt` | Should move into `ResumeService.persist_processed_resume` (or a step immediately before it) so **both** flows populate this audit table, not just bulk | Low-medium — currently an asymmetry, not a duplication, but surfaces naturally once persistence is the single shared call site | Small |

---

## SECTION 4 — SHARED PIPELINE ANALYSIS

**Can `ResumeProcessingPipeline` become the single processing engine? Yes — with one structural precondition, not a redesign.**

### What currently prevents Bulk from calling it directly

`ResumeProcessingPipeline.run()` requires `resume_id` and `candidate_id` **as inputs** — it assumes both rows already exist (`app/services/resume/resume_processing_pipeline.py`, `run()` signature: `task_id, resume_id, candidate_id, file_path, source_format, attempt_number`). Its first stage is `TEXT_EXTRACTION`, downloading a file whose `Resume` row is already in the database.

Bulk cannot satisfy this precondition until *after* it has already:
1. Downloaded the file from its staged ZIP-entry storage path
2. Extracted raw text
3. Run AI extraction to learn `full_name`/`email`
4. Resolved/created the `Candidate` via `CandidateService.get_or_create`
5. Created the `Resume` row

In other words: bulk's identity-discovery requirement means **text extraction and AI extraction must happen once *before* `Resume`/`Candidate` exist, and the rest of the pipeline (from `JSON_VALIDATION` or `SKILL_NORMALIZATION` onward) can run after**. This isn't a flaw in `ResumeProcessingPipeline`'s design — it's a correct reflection of the fact that individual upload always has identity up front (the recruiter typed it into a form) and bulk never does.

### What inputs are missing

None that can't be supplied — bulk already computes everything `ResumeProcessingPipeline.run()` needs (`resume_id`, `candidate_id`, `file_path`, `source_format`) by the time its own `Resume` row is created. The gap is architectural (bulk does its own extraction *before* calling anything pipeline-shaped), not a missing data field.

### What dependencies differ

1. **AI extraction service** — `ResumeProcessingPipeline` hardcodes `extraction_service: GeminiExtractionService` in its constructor and calls it with `RESUME_SYSTEM_PROMPT`/`ResumeExtractionGenerationSchema`. Bulk needs the *same* class/prompt/schema (see Section 3) run once, standalone, before the pipeline's remaining stages — meaning AI extraction needs to be **callable independently of the full `run()` loop**, which it currently isn't (it's a private `_run_ai_extraction` method operating on a `ResumeProcessingContext` that isn't constructed until `resume_id`/`candidate_id` are known).
2. **Context-building order** — `ResumeProcessingContext` is built once, up front, with `resume_id`/`candidate_id` as required (non-optional) fields. Bulk needs a context that can exist *before* those are known, or a two-phase construction.
3. **Checkpoint/context serialization** — `ResumeProcessingPipeline.run()` deliberately runs every stage *without* `context=`/`checkpoint_repo=` args to `StageExecutionService.run_stage()`, because `context_serializer.to_dict()` (`app/services/jd/context_serializer.py`) is hardcoded to `JDProcessingContext`'s fields (`context.title`, `context.jurisdiction`, etc.) — passing a `ResumeProcessingContext` into it would crash on the first failed stage. **This is a pre-existing gap in the individual pipeline itself**, not something bulk introduces: neither flow gets true mid-run checkpoint-resume for Resume documents today (a retried task re-runs every stage from the top). Unifying onto one pipeline doesn't fix this on its own — it would need a `document_type`-dispatched serializer (or a Resume-specific one) as a companion piece of work, orthogonal to this migration but worth doing at the same time since both flows would benefit identically.

### Minimal changes needed

**`ResumeProcessingPipeline.run()` stays a single method — no `run_pre_identity()`/`run_post_identity()` split.** Instead, it becomes capable of accepting an already-partially-populated context and skipping whatever stages that context shows are already done, using a mechanism this codebase already has in production for a different reason.

**Existing precedent:** `JDProcessingPipeline.run()` (`app/services/jd/jd_processing_pipeline.py`) already runs its 7 stages through a single loop where, for each stage, it calls `_should_skip_stage(resume_point, stage)` (`app/services/jd/jd_processing_pipeline.py:200-201`) to decide between `self.stage_tracker.skip_stage(...)` and `self.stage_tracker.run_stage(...)` — both are real, existing methods on `StageExecutionService` (`skip_stage` writes a `SKIPPED`-status row instead of re-running the stage function). JD built this for checkpoint-based retry-resume (a retried task skips stages that already succeeded before the failure point), but the mechanism is generic: *"does this stage's output already exist? If yes, skip; if no, run."* That's exactly what bulk needs, just with a different trigger for "already exists" — not a resumed checkpoint, but work bulk already did earlier in the same task, before identity was known.

**Concrete change to `ResumeProcessingPipeline.run()`:**
1. Add one new optional parameter: `initial_context: ResumeProcessingContext | None = None`. If provided, `run()` uses it instead of building a fresh context; `resume_id`/`candidate_id` are still set on it explicitly (so a context built before identity was known gets those two fields filled in at call time).
2. For each of the 7 stages, before calling `stage_tracker.run_stage(...)`, check whether that stage's expected output attribute is already non-`None` on the context (e.g. `context.raw_text is not None` for `TEXT_EXTRACTION`, `context.validated_extraction is not None` for `JSON_VALIDATION`). If so, call `stage_tracker.skip_stage(...)` instead — mirroring `JDProcessingPipeline`'s `_should_skip_stage` check, just keyed on "is the value already there" rather than "is this stage before the checkpoint's failure point."
3. `ResumeProcessingContext` gains no new required fields — its existing progressively-populated fields (`raw_text`, `cleaned_text`, `raw_extraction`, `validated_extraction`) are exactly what this check needs; only `resume_id`/`candidate_id` need to become assignable after construction rather than frozen at construction time (a small dataclass adjustment, not a redesign).

**How each caller uses the single `run()`:**
- **Individual upload** (`process_resume_document`): calls `run()` exactly as it does today — no `initial_context`, every stage's output starts `None`, every stage actually runs. Zero behavior change.
- **Bulk** (`parse_bulk_upload_file`): still has to perform text-extraction and AI-extraction *before* `Candidate`/`Resume` exist — that architectural reality doesn't go away regardless of how many methods `ResumeProcessingPipeline` exposes (Section 4's opening point still holds). It does so by calling the pipeline's own stage methods directly (`pipeline._run_text_extraction(context)`, `pipeline._run_ai_extraction(context)`, etc. — the exact same private methods `run()` itself calls, so there is still only one implementation of each stage's logic), wrapped in the same `StageExecutionService.run_stage()` calls bulk needs to adopt anyway (Section 5). Once identity is resolved and `Resume`/`Candidate` are created, bulk makes **one call** to `pipeline.run(task_id=..., resume_id=..., candidate_id=..., file_path=..., source_format=..., initial_context=context)` — the same single method individual upload calls — and because `context.raw_text`/`cleaned_text`/`raw_extraction`/`validated_extraction` are already populated, `run()`'s loop calls `skip_stage()` for those four (recording in `document_processing_stage_executions` that they happened, just not inside this particular call) and `run_stage()` for real on the remaining three (`SKILL_NORMALIZATION`/`EMBEDDING_GENERATION`/`PERSISTENCE`).

This is a small, additive change to `ResumeProcessingPipeline` — one new optional parameter and a skip-check per stage, modeled directly on a pattern already proven in `JDProcessingPipeline` — not a rewrite, not a second method, and not a change to any of the 7 stages' actual logic.

---

## SECTION 5 — STAGEEXECUTIONSERVICE ANALYSIS

**Can Bulk replace `_run_stage()` with `StageExecutionService`? Yes, cleanly — this is the lowest-risk, highest-value part of the whole migration.**

### Benefits

- Bulk-origin resumes get the same per-stage audit trail (`document_processing_stage_executions`) individual-upload resumes already get — durations, exact failure stage, retry attempt history, all queryable by `task_id`.
- Enables building a per-file bulk status-polling endpoint in the future (`GET /bulk-uploads/{job_id}/files/{file_id}/status`, mirroring individual's `GET /resumes/processing-status/{task_id}`) — impossible today since no such data exists for bulk files.
- Removes the currently-duplicated `_run_stage()` helper entirely — one fewer retry-wrapping implementation to maintain.
- Closes the newly-identified gap from yesterday's fix: because `StageExecutionService.run_stage()` records `FAILED` at the stage level regardless of what the caller does afterward, adopting it doesn't *by itself* fix the "`resume.parse_status` stuck at `PENDING`" risk in bulk's `SKILL_NORMALIZATION`/`EMBEDDING_GENERATION`/`PERSISTENCE` stages — but it does make the failure **visible and queryable** even before that separate fix lands, which today it is not (currently only `bulk_upload_job_files.status` reflects a permanent failure; there's no stage-level record at all).

### Required inputs

`StageExecutionService.run_stage(task_id, document_type, stage, fn, attempt_number, context=None, checkpoint_repo=None)` — bulk already has `task_id`, `DocumentType.RESUME` (imported and used elsewhere in the same file already), a `ProcessingStage` value per stage, and a zero-arg callable per stage (its lambdas today). No new inputs need to be invented; it's a drop-in replacement for the equivalent `_run_stage(checkpoint_repo, task_id, stage, context_data, fn)` calls.

### Missing context

The only missing piece is the same one noted in Section 4: passing `context=`/`checkpoint_repo=` for true mid-run resume isn't safe yet because `context_serializer.to_dict()` is JD-only. **Recommendation: omit `context=`/`checkpoint_repo=` for bulk too, exactly as individual upload already does** — this keeps `run_stage`'s failure-tracking benefit without inheriting a crash risk, and preserves bulk's existing checkpoint mechanism (`CheckpointRepository`, already document-type-agnostic and already working) as a separate, parallel mechanism, unchanged.

### Database impact

**None.** `document_processing_stage_executions` already exists, is already document-type-agnostic (`document_type` column accepts `DocumentType.RESUME` already, since individual upload writes it today), and its own docstring anticipates exactly this reuse. No new table, column, or index is needed.

### Migration complexity

**Low.** This is a mechanical replacement: swap each `_run_stage(checkpoint_repo, task_id, STAGE, context_data, fn)` call in `parse_bulk_upload_file` for `stage_tracker.run_stage(task_id, DocumentType.RESUME, STAGE, fn, attempt_number=attempt_number)`, where `stage_tracker = StageExecutionService(DocumentProcessingRepository(stage_db))` — following `process_resume_document`'s existing pattern of a **second, separate DB session** (`stage_db`) for stage-tracking writes, isolated from the business-write session (`db`), exactly as individual upload already does and documents ("Stage tracking runs on its own session... same reasoning as process_jd_document").

---

## SECTION 6 — GEMINI ANALYSIS

### `GeminiExtractionService` vs `GeminiResumeExtractionService`

**They are not identical, and the differences materially affect extraction quality — this is the single most consequential finding in this analysis.**

| Aspect | `GeminiExtractionService` (used by individual, via prompt/schema override) | `GeminiResumeExtractionService` (bulk-only) |
|---|---|---|
| Class location | `app/services/extractions/gemini_extraction_service.py` — genuinely a **JD**-oriented class (default prompt/schema are JD's; its `extract_raw` f-string template literally says `"Job Description:"` before the content, unconditionally, even when Resume prompt/schema are passed in as overrides) | `app/services/extractions/gemini_resume_extraction_service.py` — purpose-built for Resume, template says `"Resume:"` |
| Prompt used | `RESUME_SYSTEM_PROMPT` (passed as override) — richer: requests `is_current`/`is_internship`/`is_volunteer` booleans per work-experience entry, `certifications` as a flat list, `graduation_year` as an integer | `SYSTEM_PROMPT` (its own default) — narrower: no `is_current`/`is_internship`/`is_volunteer` request at all (so `WorkExperience` fields default to `False` for every bulk-parsed resume), no `certifications` in its JSON template, and its own example uses the key `"year"` for graduation — **which does not match `EducationEntry.graduation_year`**, a self-inconsistency inside bulk's own prompt |
| Identity fields requested | Not requested (individual doesn't need them from the document) | `full_name`/`email`/`phone` explicitly requested — required for bulk's parse-first identity resolution |
| Generation mode | **Schema-constrained**: passes `response_schema=ResumeExtractionGenerationSchema` to Gemini's `generate_content` config — Gemini's structured-output mode enforces the shape server-side | **Freeform JSON mode**: only `response_mime_type: "application/json"` is passed, no `response_schema` — shape compliance relies entirely on the prompt text being followed correctly |
| Output type | `ResumeExtractionResponse` (via `extract_raw` + manual `.model_validate()` at the pipeline's separate `JSON_VALIDATION` stage) | `ResumeExtractionResponse` (validated *inside* `extract()` itself — no separate validation stage) |

### Are they identical? No.
### Different prompts? Yes — genuinely different content, not just formatting.
### Different schemas? No — both ultimately produce `ResumeExtractionResponse`, but bulk's prompt doesn't ask Gemini to populate several fields that schema supports (`is_current`, `is_internship`, `is_volunteer`, `certifications`), so those fields are effectively always empty/default for bulk-origin resumes today, even though the database column shape has room for them.
### Different outputs? Yes, in practice — same shape, less-complete data, and less-reliable shape compliance (freeform vs. schema-constrained generation).
### Can one replace the other? Yes, in one direction only: `GeminiResumeExtractionService` should be replaced by a call into `GeminiExtractionService.extract_raw(text, prompt=RESUME_SYSTEM_PROMPT, response_schema=ResumeExtractionGenerationSchema)` — the same call individual upload already makes — **provided `RESUME_SYSTEM_PROMPT` is first extended to also request `full_name`/`email`/`phone`** (harmless for individual upload, which already ignores those three fields on the response it gets back).
### Should one be removed? Yes — `GeminiResumeExtractionService` and `SYSTEM_PROMPT` (the bulk-only prompt) should both be retired once `RESUME_SYSTEM_PROMPT` covers identity fields, since keeping two Resume-extraction prompts around is exactly the "two implementations of one responsibility" pattern this whole migration exists to eliminate — and the one being removed is measurably the weaker of the two.

---

## SECTION 7 — MIGRATION PLAN

### Phase 1 — Shared abstractions, no functional change
**Goal:** Make the pieces reusable without changing what either flow currently does.
- Extend `RESUME_SYSTEM_PROMPT` to request `full_name`/`email`/`phone` (additive — individual upload's behavior is unaffected since it already ignores those fields on its response object)
- Add the skip-capable `initial_context` parameter to `ResumeProcessingPipeline.run()` (Section 4) — modeled on `JDProcessingPipeline`'s existing `_should_skip_stage`/`skip_stage` pattern. `run()` remains a single method; `process_resume_document`'s call site is unchanged (it never passes `initial_context`, so every stage runs exactly as today) — **no behavior change for individual upload**
- Add a `record_parse_attempt` call inside `ResumeService.persist_processed_resume` (or immediately before it) so both flows populate `resume_parse_attempts` symmetrically
- **Files affected:** `app/prompts/resume_extraction_prompt.py`, `app/services/resume/resume_processing_pipeline.py`, `app/services/resume/resume_processing_context.py`, `app/services/resume/resume_service.py`
- **Classes affected:** `ResumeProcessingPipeline`, `ResumeProcessingContext`, `ResumeService`
- **Risks:** Prompt change could shift individual-upload extraction results for edge cases — needs a before/after diff on a sample resume set. Adding the skip-check risks a subtle bug if a stage's "already done" check (`is not None`) ever fires on legitimately-empty-but-valid data (e.g. an empty `skills: []` list is not `None`, so this must key off the *object* being present — `validated_extraction is not None` — not off whether its contents are non-empty).
- **Rollback:** Revert the prompt string and the `initial_context` parameter independently — both are self-contained diffs with no schema/data dependency, and individual upload's call site never changes either way.

### Phase 2 — Replace duplicated Gemini extraction
**Goal:** Bulk calls the same extraction class/prompt/schema individual upload uses.
- `parse_bulk_upload_file`'s `AI_EXTRACTION` stage switches from `GeminiResumeExtractionService.extract(cleaned_text)` to `GeminiExtractionService.extract_raw(cleaned_text, prompt=RESUME_SYSTEM_PROMPT, response_schema=ResumeExtractionGenerationSchema)` + `ResumeExtractionResponse.model_validate(...)` as its own explicit `JSON_VALIDATION` stage (bulk currently has no separate validation stage — folding it in now sets up Phase 3 cleanly)
- **Files affected:** `app/tasks/bulk_upload_tasks.py`
- **Classes affected:** none new — `GeminiExtractionService` reused as-is
- **Risks:** Schema-constrained generation could reject a resume shape freeform mode previously tolerated — needs regression testing against a sample of previously-bulk-uploaded resumes (re-run through the new path in a non-prod environment). This is the phase most likely to change real extraction *output* for bulk resumes (in the intended direction — richer data — but still a behavior change worth flagging to stakeholders before rollout).
- **Rollback:** Revert the single call-site change in `parse_bulk_upload_file`; `GeminiResumeExtractionService`/`SYSTEM_PROMPT` are not yet deleted at this phase, so rollback is a one-line diff.

### Phase 3 — Move Bulk onto `ResumeProcessingPipeline`'s shared stages
**Goal:** Bulk stops hand-rolling `SKILL_NORMALIZATION`/`EMBEDDING_GENERATION`/`PERSISTENCE` and instead makes one call into the same `ResumeProcessingPipeline.run()` individual upload uses, and replaces `_run_stage()` with `StageExecutionService.run_stage()` for its own pre-identity stages too (Section 5).
- `parse_bulk_upload_file`: its `STORAGE`/`TEXT_EXTRACTION`/`TEXT_CLEANING`/`AI_EXTRACTION` stages call the pipeline's own stage methods directly (`pipeline._run_text_extraction(context)` etc. — same implementation individual upload runs, invoked via `stage_tracker.run_stage(...)` at bulk's own call site, since identity isn't known yet and `run()` itself can't be called until it is)
- After `Resume`/`Candidate` creation, bulk calls `pipeline.run(task_id=..., resume_id=..., candidate_id=..., file_path=..., source_format=..., initial_context=context)` **once** — the same single method individual upload calls — and because `context` already has `raw_text`/`cleaned_text`/`raw_extraction`/`validated_extraction` populated, `run()`'s own skip-check (Phase 1) causes it to skip those four stages and genuinely execute `SKILL_NORMALIZATION`/`EMBEDDING_GENERATION`/`PERSISTENCE`
- Introduce a second `stage_db` session in `parse_bulk_upload_file`, mirroring `process_resume_document`'s dual-session pattern
- **Files affected:** `app/tasks/bulk_upload_tasks.py`, `app/services/resume/resume_processing_pipeline.py` (no further change beyond Phase 1's `initial_context` addition — this phase only changes *callers*)
- **Classes affected:** `ResumeProcessingPipeline` (consumed, not modified further), `StageExecutionService` (reused, not modified)
- **Risks:** This is the highest-touch phase — it changes bulk's transaction/session shape. Needs careful testing of the existing atomic per-file claim (`try_start_processing`) and job-counter increments, which must remain unaffected by the pipeline hand-off. Also needs a check that `run()`'s single `skip_stage()`/`run_stage()` loop can't be accidentally invoked twice for the same stage across bulk's two call sites (its own pre-identity calls, then `run()`'s post-identity loop) — the stage list each half touches must stay disjoint.
- **Rollback:** Revert `parse_bulk_upload_file` to Phase 2's state (still uses its own `_run_stage` calls for these three stages, just with the new Gemini call already in place) — a clean intermediate rollback point exists because Phase 2 is independently shippable.

### Phase 4 — Remove duplicate code
**Goal:** Delete what's no longer referenced.
- Delete `app/services/extractions/gemini_resume_extraction_service.py`, `SYSTEM_PROMPT` from `app/prompts/resume_extraction_prompt.py`, and the module-level `_run_stage()` function from `bulk_upload_tasks.py`
- **Files affected:** `app/services/extractions/gemini_resume_extraction_service.py` (deleted), `app/prompts/resume_extraction_prompt.py`, `app/tasks/bulk_upload_tasks.py`
- **Risks:** Low, provided Phase 3 has been in production long enough to be confident nothing else imports the deleted class (a repo-wide grep confirms today that `GeminiResumeExtractionService` has exactly one call site — `bulk_upload_tasks.py` — but re-verify at deletion time in case something else picks it up in the interim).
- **Rollback:** Restore the deleted file/prompt from version control — trivial, since nothing else depends on them by this phase.

### Phase 5 — Regression testing
**Goal:** Confirm parity and no regressions across both flows.
- Re-run the individual-upload smoke test (already exercised live this session) end-to-end
- Re-run a bulk ZIP upload with a mix of valid/duplicate/unparseable files (mirroring the 6-file test job run earlier this session) and confirm: every successful file has `candidate_skills`, `resume_embeddings`, `document_processing_stage_executions` rows (the new addition), and `resume_parse_attempts` (now symmetric)
- Confirm `parser_version` is consistently `ResumeService.PARSER_VERSION` for every resume, individual or bulk in origin
- Confirm a forced permanent-stage failure correctly flips `resume.parse_status` to `FAILED` for bulk too (closing the gap identified in this session's live testing)
- **Files affected:** none (test-only phase)
- **Risks:** None beyond normal test-writing effort
- **Rollback:** N/A

---

## SECTION 8 — FILE IMPACT ANALYSIS

| File | Reason | What changes | Complexity | Risk | Dependencies |
|---|---|---|---|---|---|
| `app/prompts/resume_extraction_prompt.py` | Unify on one Resume prompt | `RESUME_SYSTEM_PROMPT` gains `full_name`/`email`/`phone` request; `SYSTEM_PROMPT` deleted in Phase 4 | Low | Medium (extraction-output shift) | None |
| `app/services/resume/resume_processing_pipeline.py` | Enable skip-capable single-method execution | `run()` gains one new optional parameter (`initial_context`) and a per-stage skip-check (`is this stage's output already present?` → `skip_stage()` vs `run_stage()`), modeled on `JDProcessingPipeline`'s existing `_should_skip_stage` pattern — **`run()` stays one method**, no split | Low-Medium | Low-Medium (core shared class, but additive/backward-compatible — individual upload's existing call site needs zero changes) | `ResumeProcessingContext` |
| `app/services/resume/resume_processing_context.py` | Support late-bound identity fields | `resume_id`/`candidate_id` become settable after construction (small dataclass adjustment) so a context built during bulk's pre-identity work can have them filled in once known | Low | Low | `ResumeProcessingPipeline` |
| `app/tasks/resume_processing_tasks.py` | None | No change — `process_resume_document` calls `pipeline.run(...)` exactly as it does today | None | None | `ResumeProcessingPipeline` |
| `app/tasks/bulk_upload_tasks.py` | Central migration target | AI extraction call swapped (Phase 2); pre-identity stages (`STORAGE`/`TEXT_EXTRACTION`/`TEXT_CLEANING`/`AI_EXTRACTION`) call the pipeline's own stage methods directly via `StageExecutionService.run_stage()`; post-identity stages replaced by one `pipeline.run(..., initial_context=context)` call (Phase 3); `_run_stage()` helper deleted (Phase 4); second `stage_db` session added | High | Medium-High (most active file in this session's work, and the one with the still-open B9 orphan-cleanup issue — changes must stay scoped away from that code path) | `ResumeProcessingPipeline`, `StageExecutionService`, `GeminiExtractionService` |
| `app/services/extractions/gemini_resume_extraction_service.py` | Superseded | Deleted in Phase 4 | Trivial | Low | None once Phase 2 lands |
| `app/services/resume/resume_service.py` | Symmetric `resume_parse_attempts` | `persist_processed_resume` gains a `record_parse_attempt` call | Low | Low | `ResumeRepository.record_parse_attempt` (already exists) |
| `app/repositories/resume_repository.py` | Support point above | Possibly none — `record_parse_attempt` already exists and is generic | None expected | None | — |

---

## SECTION 9 — API IMPACT

- **No REST API contract changes required for Phases 1–4.** `POST /resumes`, `POST /bulk-uploads`, `GET /bulk-uploads/{id}`, and `GET /resumes/processing-status/{task_id}` all keep their current request/response schemas.
- **Frontend:** No required changes. A *future*, optional addition (not part of this migration) would be a per-file bulk status-polling endpoint, now made possible by Phase 3's stage-tracking data — but that's a new feature, not a consequence of this refactor.
- **Response payloads:** Unchanged. `BulkUploadJobDetailResponse`/`BulkUploadJobFileItem` (`app/schemas/bulk_upload/response.py`) don't currently expose stage-level detail and don't need to for this migration to be complete.
- **Polling:** Individual upload's polling endpoint behavior is unchanged. Bulk gains *queryable* stage data it didn't have before, but no new endpoint is required by this plan — that's a follow-on opportunity.
- **Celery task names:** Unchanged — `resume.process_document` and `bulk_upload.parse_file` keep their registered names (`@celery_app.task(name=...)`) throughout. Internal call structure changes; the task's external identity does not.

---

## SECTION 10 — DATABASE IMPACT

**No new tables, columns, indexes, or constraints are required.**

- `document_processing_stage_executions` already exists, already accepts `DocumentType.RESUME`, and is already unindexed beyond its `UniqueConstraint("task_id", "stage", "attempt_number")` and implicit PK — sufficient for bulk's additional write volume (one row per file per stage, same shape individual upload already produces).
- `resume_parse_attempts` already exists; Phase 1 only adds a second call site (individual upload) to an existing, unmodified schema.
- `document_processing_checkpoints` is unaffected — both flows continue using it exactly as today.
- No migration file is needed for any phase of this plan.

---

## SECTION 11 — RISKS

**Functional risks:**
- Phase 2's switch to schema-constrained Gemini generation could reject resume text shapes the freeform mode previously accepted — needs a real regression pass, not just unit tests, since Gemini's behavior isn't fully deterministic across prompt/schema changes.
- Phase 1's prompt extension is additive but any prompt change to a production LLM call carries some risk of subtly different extraction for *existing* individual-upload traffic, not just bulk.

**Performance risks:**
- Low. Adding `StageExecutionService.run_stage()` calls to bulk's per-file task adds one extra DB write per stage (7 stages × N files) — the same overhead individual upload already carries per resume, so this is "bulk catching up to individual's existing cost," not a new cost class.

**Concurrency risks:**
- Phase 3 introduces a second `stage_db` session into `parse_bulk_upload_file`, alongside the existing atomic `try_start_processing` claim and atomic job-counter increments (`increment_processed_count` etc.). These must remain on the *business* session (`db`), not the new stage-tracking session, exactly as `process_resume_document` already separates the two — get this wrong and job-counter atomicity could be compromised.

**Retry risks:**
- None beyond what exists today — `RetryDriver`/`error_classifier`/`STAGE_POLICIES` are reused unchanged throughout every phase.
- Separately (already true today, not introduced by this plan, but relevant context for whoever implements Phase 3): a permanent failure inside `SKILL_NORMALIZATION`/`EMBEDDING_GENERATION`/`PERSISTENCE` currently leaves `resume.parse_status` stuck at `PENDING` in bulk's `except StageExecutionError` branch (confirmed live in this session) — Phase 3's migration to `ResumeProcessingPipeline` doesn't automatically fix this unless the exception-handling branch is also updated to call `mark_parse_failed`, mirroring the fix already applied to individual upload's equivalent branch. Recommend folding that fix into Phase 3, since it touches the same code region.

**Data consistency risks:**
- Low — `ResumeService.persist_processed_resume` is already the single write-path for `parsed_json`/`candidate_skills`/`resume_embeddings` for both flows (since yesterday's fix); this migration doesn't change *what* gets written, only *how the calling code is structured* getting there.

**Rollback risks:**
- Each phase is independently revertible (see per-phase rollback notes in Section 7) because no phase requires a database migration — a `git revert` of any phase's commit is sufficient, with no data backfill/cleanup needed.

---

## SECTION 12 — FINAL RECOMMENDATION

**Yes — `ResumeProcessingPipeline` should become the single processing engine for both upload paths, from `AI_EXTRACTION` (or `TEXT_EXTRACTION`, for bulk) onward.**

**Why:**
1. Every stage from `SKILL_NORMALIZATION` onward is *already* identical between the two flows (confirmed: same classes, same methods, same call signatures, verified live in this session's testing) — the only reason it isn't already the same *code path* is that bulk reimplements the calling/tracking layer around those identical components instead of reusing `StageExecutionService`/`ResumeProcessingPipeline` directly.
2. The schema was explicitly built anticipating this: `DocumentProcessingStageExecution`'s own docstring states it's "document-type-agnostic so a future Resume pipeline reuses it as-is," and `DocumentProcessingRepository`/`CheckpointRepository`/`error_classifier`/`STAGE_POLICIES` all already key on `task_id`/exception-type/`ProcessingStage` rather than anything document-type-specific.
3. Unifying closes a real, freshly-discovered gap (Gemini extraction quality — Section 6) that a "just share more helper functions" approach would not have surfaced, since it required actually comparing the two prompts side-by-side.
4. It eliminates an entire class of "individual gets a bugfix, bulk doesn't" incidents — exactly the pattern this session repeatedly encountered (missing skill/embedding data, now the `parse_status`-stuck-at-`PENDING` risk) — by construction, since there would no longer be two implementations to independently patch.

**What must remain unique to Bulk forever** (this migration does not, and should not, try to eliminate these):
- **ZIP validation and extraction** (`ZipValidationService`, `extract_bulk_upload_zip`) — there is no individual-upload equivalent to unify with; this is genuinely bulk-only work.
- **Bulk job/file tracking** (`bulk_upload_jobs`, `bulk_upload_job_files`, `BulkUploadJobRepository`/`BulkUploadJobFileRepository`, the atomic `try_start_processing` claim, job-counter increments, `_maybe_finalize_job`) — this orchestration layer has no individual-upload counterpart and shouldn't be forced into one.
- **Identity discovery** (running `TEXT_EXTRACTION`→`AI_EXTRACTION` *before* `Candidate`/`Resume` exist) — this is bulk's defining architectural difference from individual upload and is correct as designed; the migration's job is to make what happens *after* identity is known identical, not to pretend bulk can somehow know identity up front the way a form submission does.
- **Duplicate/orphan-cleanup handling around ZIP extraction** (the still-open B9 issue) — entirely orthogonal to this migration and must not be touched by it.
