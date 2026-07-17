# JD Pipeline → Resume Readiness: Codebase Analysis

Read-only analysis. No code changes proposed here — findings only, with exact file/class/function references.

---

## 1. End-to-end pipeline map

Two stages run synchronously in the API route before queueing; the remaining six run inside the Celery task via `JDProcessingPipeline.run()`.

| # | Stage (enum) | Class/Function | Input | Output | Side effects | External deps | Generic / JD-specific |
|---|---|---|---|---|---|---|---|
| 0a | VALIDATION | `jd_routes.py` (`service.validate_upload_type`, inline for text path) | `UploadFile` / raw text | pass/raise | none (pre-check) | none | Generic (file-type check) |
| 0b | STORAGE | `service.validate_and_store_file` (`jd_routes.py:188-191`) | file bytes | storage key | writes to object storage (S3, prefix `S3_JD_PREFIX`) | AWS S3 | Generic mechanism, **JD-specific S3 prefix constant** |
| 1 | TEXT_EXTRACTION | `JDProcessingPipeline._run_text_extraction` (`jd_processing_pipeline.py:211`) | `context.raw_text` or downloaded file + `JDSourceFormat` | `context.text` | `StorageService.download_file` | S3 | Generic logic, but signature takes `JDSourceFormat` (JD-namespaced enum) — **coupling gap** |
| — | Duplicate check (untracked stage) | inline in `run()` (`jd_processing_pipeline.py:158-169`) | content hash | short-circuit | `jd_repository.get_by_content_hash` | Postgres | JD-specific (queries `job_descriptions`) |
| 2 | TEXT_CLEANING | `PreprocessingService.normalize` (`preprocessing_service.py:11-47`) | `context.text` | `context.cleaned_text` | none | none | **Fully generic** (Unicode normalize, whitespace, bullets, lowercase) |
| 3 | AI_EXTRACTION | `GeminiExtractionService.extract_raw` (`gemini_extraction_service.py:17-45`) | `cleaned_text` | raw JSON dict | Gemini API call | Google `genai` SDK | **Hardcoded to JD** (prompt + response schema) |
| 4 | JSON_VALIDATION | `JDExtractionResponse.model_validate` (`jd_processing_pipeline.py:224-225`) | raw JSON | `JDExtractionResponse` | none | none | **Hardcoded to JD** (schema class name + fields) |
| 5 | SKILL_NORMALIZATION | `SkillNormalizationService.normalize_skills` (`skill_normalization_service.py:76`) | `required_skills`, `preferred_skills` (lists of str) | normalized/matched skills | reads/writes `skill_ontology`, `unknown_skills` | Postgres, embedding model | **Fully generic** — takes plain string lists |
| 6 | EMBEDDING_GENERATION | `EmbeddingService.build_canonical_embedding_text` + `generate_embedding` (`embedding_service.py:29-65`, `:9-27`) | validated `JDExtractionResponse` + title | embedding vector | loads `all-MiniLM-L6-v2` locally | sentence-transformers | **Hardcoded to JD** (builder reads JD field names/labels) |
| 7 | PERSISTENCE | `JDService.persist_processed_jd` (`jd_service.py`, called at `jd_processing_pipeline.py:239`) | context (extraction, embedding, skills) | DB rows | writes `job_descriptions`, `jd_embeddings`, `jd_skills`, `jd_unknown_skills` | Postgres | **Hardcoded to JD** |

Every stage also writes to the generic tracking tables (`document_processing_stage_executions`, checkpoints) via `StageExecutionService`, regardless of which business logic ran.

---

## 2. Entry points

All in `app/api/routes/jd_routes.py` (prefix `/job-descriptions`):

| Function | Method | Route | Trigger |
|---|---|---|---|
| `create_job_description` (:116) | POST | `/job-descriptions` | `process_jd_document.apply_async(...)` (:135) |
| `create_job_description_from_file` (:163) | POST | `/job-descriptions/from-file` | `process_jd_document.apply_async(...)` (:195) |
| `update_job_description` (:321) | PUT | `/job-descriptions/{jd_id}` | conditional reprocess via `_queue_reprocess()` (:57) |
| `update_job_description_from_file` (:348) | PUT | `/job-descriptions/{jd_id}/from-file` | always reprocesses |
| `get_jd_processing_status` (:223) | GET | `/job-descriptions/processing-status/{task_id}` | polling only, not a trigger |

Actual pipeline execution: Celery task `process_jd_document` (`app/tasks/jd_processing_tasks.py:34`, task name `"jd.process_document"`), which instantiates `JDProcessingPipeline` and calls `.run()`. No other consumers/schedulers exist.

**Request DTOs** (`app/schemas/jd/request.py`): `CreateJDRequest`, `UpdateJDRequest` — fields `title`, `raw_text`, `jurisdiction`, `min_experience_years`, `max_experience_years`, `notice_period`, `education_criteria`. **None contain a `document_type` field.**

**Does `document_type` exist anywhere?** Yes, but disconnected from the request layer:
- `app/models/async_tasks.py:35-37`: `class DocumentType(enum.Enum): JD = "JD"; RESUME = "RESUME"` — already has both values.
- `app/services/jd/jd_processing_context.py:33`: `document_type: DocumentType = DocumentType.JD` — hardcoded default, never overridden.
- Migration `alembic/versions/4fd0a3c4f90d_...py:27`: DB enum `document_type_enum` already includes `'JD','RESUME'`.
- "JD-ness" today is determined **structurally** (which route/task/pipeline class you call), not by a runtime flag anywhere in the request/response contract.

---

## 3. Stage-by-stage JD coupling classification

| Stage | Implementation today | Classification |
|---|---|---|
| Validation | File-type check in route layer, generic | **Fully generic** |
| Storage | Generic S3 upload mechanism, but uses JD-specific prefix constant (`S3_JD_PREFIX`) | **Generic with config change** (swap prefix constant) |
| Text Extraction | `TextExtractionService.extract(file_content, source_format: JDSourceFormat)` — logic is generic, but parameter type is JD-namespaced | **Generic with config change** (needs a document-agnostic format enum, or reuse) |
| Text Cleaning | `PreprocessingService.normalize()` — pure text normalization, no JD assumptions | **Fully generic** (caveat: blanket lowercasing may be lossy for resumes with acronyms/proper nouns) |
| AI Structured JSON Extraction | Single hardcoded `SYSTEM_PROMPT` (`jd_extraction_prompt.py`) + single schema (`JDExtractionResponse`) baked into `GeminiExtractionService` | **Requires resume-specific strategy** (new prompt + new schema; the LLM-call mechanics/client are reusable) |
| JSON Validation | `JDExtractionResponse.model_validate(...)` called directly by class name in the pipeline | **Requires resume-specific strategy** (new Pydantic schema class + validators) |
| Skill Ontology & Normalization | `SkillNormalizationService.normalize_skills(required, preferred)` operates on plain string lists against a shared `skill_ontology` table | **Fully generic** — reusable as-is, callable with resume-extracted skill lists |
| Embedding Generation | `EmbeddingService.build_canonical_embedding_text()` reads JD field names/labels directly off `JDExtractionResponse`; `generate_embedding()` itself (model call) is generic | **Requires resume-specific strategy** for the text-builder; the embedding model/call is **fully generic** |
| Persistence | `JDService.persist_processed_jd()` writes to JD-only tables (`job_descriptions`, `jd_embeddings`, `jd_skills`, `jd_unknown_skills`) | **Hardcoded to JD** — needs a parallel `ResumeService`/`ResumeRepository`, though the tracking-table layer underneath is already generic |

---

## 4. AI extraction implementation details

- **Prompt**: `app/prompts/jd_extraction_prompt.py:1-177`, constant `SYSTEM_PROMPT`. Hardcoded JD framing ("expert AI Recruitment Assistant specializing in analyzing Job Descriptions"), with section rules for skills/experience/education/responsibilities/certifications/employment_type/location/metadata. Note: `work_mode` is in the schema but **not mentioned in the prompt text at all** — effectively always null in practice.
- **Schema**: `JDExtractionResponse` defined at `app/schemas/ai/jd_extraction_response.py:50-83`. Fields: `required_skills`, `preferred_skills`, `responsibilities`, `certifications`, `experience` (nested `Experience`: min/max years), `education` (nested `Education`: degree/field), `employment_type`, `work_mode`, `location`, `metadata`. A **second, near-duplicate schema** `JDExtractionGenerationSchema` (lines 86-104) exists solely because Gemini's structured-output mode rejects open `dict` fields — it omits `metadata` and must be manually kept in sync with `JDExtractionResponse`.
- **Fields the task asked about that do NOT exist in this implementation**: `job_title` and `languages` — confirmed zero hits codebase-wide. (The route/DB uses `title`, not `job_title`, at the request/DB level, separate from the AI-extracted object.)
- **Usage sites per field**:
  - `required_skills`/`preferred_skills` → schema (`:51-52`), dedupe validator (`:76-83`), prompt (`:59-84`), skill normalization call (`jd_processing_pipeline.py:227-230`), embedding text (`embedding_service.py:37-40`), DB column `JobDescription.required_skills` JSONB (`job_descriptions.py:65`), API response `GetJDResponse.required_skills` (`response.py:13`).
  - `responsibilities`/`certifications` → schema (`:53-54`), prompt (`:131-137`), embedding text (`:41-44`); otherwise only inside the raw `extracted_json` blob — no dedicated DB columns or API fields.
  - `experience` → AI-extracted sub-object is **separate and never reconciled** with the user-submitted `min_experience_years`/`max_experience_years` on `CreateJDRequest`/`JobDescription` DB columns — persistence uses the user-submitted values, not the AI-extracted ones (`jd_processing_pipeline.py:246-268`).
  - `education` → similarly duplicated: AI-extracted `Education` vs. user-submitted `EducationCriteria` (request field, DB column `education_criteria` JSONB) — two parallel concepts, not unified.
  - `employment_type`, `work_mode`, `location` → schema + embedding text only; no DB columns, no API fields — reachable only via the raw `extracted_json` blob.
  - `metadata` → always `{}` per prompt; dropped from the Gemini-facing generation schema, restored as default on Pydantic validation.
- **LLM call mechanics**: `GeminiExtractionService` (`gemini_extraction_service.py`) uses `google.genai.Client`, model from `settings.gemini_model` (default `"gemini-flash-latest"`, `app/core/config.py:35`), structured output via `response_mime_type: application/json` + `response_schema: JDExtractionGenerationSchema`. No temperature or other generation params set. Note: `GeminiExtractionService.extract()` (with inline validation) appears to be dead code — the pipeline calls `extract_raw()` directly and validates separately in the JSON_VALIDATION stage.

---

## 5. Validation and schema binding

All validators live in `app/schemas/ai/jd_extraction_response.py`:
- `Experience.validate_years` / `validate_range` — non-negative years, min ≤ max.
- `Education.clean_optional_string` — trims/nullifies degree & field.
- `JDExtractionResponse.clean_lists` — strips/dedupes the four string-list fields.
- `JDExtractionResponse.clean_optional_string` — trims employment_type/work_mode/location.
- `JDExtractionResponse.dedupe_preferred_against_required` — cross-field "required wins" dedupe.

**Binding point**: `JDProcessingPipeline._run_json_validation` (`jd_processing_pipeline.py:224-225`) calls `JDExtractionResponse.model_validate(context.raw_extraction)` by direct class reference — the pipeline is compiled against this one schema, not a parameter.

**What would need to be parameterized for a resume schema in parallel**: the pipeline's `_run_json_validation`, `_run_ai_extraction`, and `_run_embedding_generation` methods all reference `JDExtractionResponse`/`GeminiExtractionService`/`build_canonical_embedding_text` by fixed name. There is no schema-registry or type parameter — a resume schema would need its own equivalent class plus its own pipeline/context (per the class's own documented design intent, see §8).

---

## 6. Embedding generation analysis

Exact builder: `EmbeddingService.build_canonical_embedding_text(extraction: JDExtractionResponse, title: str)` (`embedding_service.py:29-65`). Docstring states explicitly: *"Deterministic canonical text built from the validated structured JD JSON (not raw_text), used as the embedding input per spec."*

- **Source**: validated structured JSON (`JDExtractionResponse`), **not** raw or cleaned text.
- **Fields included**: `title` (param), `required_skills`, `preferred_skills`, `responsibilities`, `certifications`, `experience.min/max_experience_years`, `education.degree/field`, `employment_type`, `work_mode`, `location`. `metadata` is **not** included.
- **JD-specific parts**: every field name and label string (`"Required Skills:"`, `"Experience:"`, etc.) is hardcoded Python — no loop over a generic field list, no per-document-type template. This entire method would need a resume-specific counterpart.
- **Reusable parts**: `EmbeddingService.generate_embedding()` (the actual model inference call, `all-MiniLM-L6-v2` via sentence-transformers) is fully generic — it just takes a string. Model versioning (`EmbeddingModelVersion` DB table) and the 384-dim `pgvector` column pattern are already dual-provisioned (`jd_embeddings` and `resume_embeddings` both exist).

---

## 7. Persistence analysis

**Generic / already document-type-agnostic:**
- `document_processing_checkpoints`, `document_processing_stage_executions` (`app/models/async_tasks.py:66,152`) — both have a `document_type` enum column and nullable generic `document_id`; docstrings explicitly say *"Document-type-agnostic so a future Resume pipeline reuses it as-is."* `CheckpointRepository` and `DocumentProcessingRepository` both accept `document_type: DocumentType` as a parameter with no JD default.
- `skill_ontology`, `unknown_skills`, `skill_suggestions` — canonical skill catalog, no JD reference.
- `celery_task_log`, `dead_letter_queue` — already have both `resume_id` and `jd_id` nullable FKs.
- `resumes`, `resume_parse_attempts`, `resume_embeddings`, `candidates`, `candidate_skills` — resume-side tables **already exist in the schema** but nothing currently reads/writes them (no `ResumeRepository`, no resume pipeline).

**JD-specific (fields baked into schema):**
- `job_descriptions` (`app/models/jd/job_descriptions.py:43-86`) — JD-only columns (`title`, `required_skills` JSONB, `min/max_experience_years`, `notice_period`, `education_criteria`, `source_format`, JD lineage/versioning columns).
- `jd_embeddings`, `jd_skills`, `jd_unknown_skills` — join/embedding tables FK'd directly to `job_descriptions.id`; structurally distinct from the (already-defined but unused) `candidate_skills`/`resume_embeddings` counterparts — the two are **not unified into one polymorphic table**.

**Repositories**: `JDRepository` (`app/repositories/jd_repository.py`) is entirely JD-specific (no reusable base). `SkillRepository` mixes generic ontology methods with JD-specific ones (`create_jd_skill`, `link_unknown_skill_to_jd`) — no equivalent `create_candidate_skill` method exists yet. `CheckpointRepository`, `DocumentProcessingRepository`, `CeleryTaskLogRepository` are fully generic.

**Gap found**: `EntityType` enum (`app/enums/constants.py:54-60`, used by `AuditLog.entity_type`) has no `RESUME`/`CANDIDATE_SKILL` member — audit logging for a resume pipeline can't be modeled without an enum migration. Similarly `ActionType` has explicit `JD_*` actions but no `RESUME_*` equivalents.

**Migration risk noted**: the Alembic history references revisions (`a92163422dba`, `793ec14a7a28`, `c8f2a4d6e910`, `a41e892f4a72`) with no corresponding files — the migration graph has gaps/was squashed. Worth resolving before adding resume-table migrations on top.

---

## 8. Configuration and extensibility

- **No factory, strategy, or registry pattern exists** (`grep` for `class .*Factory|class .*Strategy|registry` across `app/` returns zero hits).
- Stage order is a **hardcoded tuple iterated in a for-loop** inside `JDProcessingPipeline.run()` (`jd_processing_pipeline.py:171-178`), not data/config-driven.
- `document_type` threads through the **tracking/telemetry layer only** (every `stage_tracker.run_stage(...)` call passes `context.document_type` for logging into `document_processing_stage_executions`) — it does **not** select which business logic runs. There is no `if document_type == RESUME` branch anywhere in the stage bodies.
- Retry policy (`app/services/document_processing/retry_policy.py`) is keyed by `ProcessingStage` (enum-keyed dict `STAGE_POLICIES`), not by document type — already shared/generic across document types.
- **The pipeline's own docstrings state the intended extension pattern explicitly** (`jd_processing_pipeline.py:37-40`, `jd_processing_context.py:13-19`): *"Concrete and JD-specific by design: when a Resume pipeline is built, it defines its own `ResumeProcessingContext` and reuses `StageExecutionService` directly, rather than sharing a base class guessed ahead of a second real caller."* This is a **documented parallel-pipeline strategy**, not a shared-abstraction strategy — i.e., the codebase's own design intent is "duplicate the orchestrator, share the infrastructure," not "parameterize one orchestrator."

**Per-document-type support today:**
| Concern | Already parameterized per document type? |
|---|---|
| Extraction prompt | No — single hardcoded `SYSTEM_PROMPT` constant |
| Extraction schema | No — single hardcoded `JDExtractionResponse` class reference |
| Validation rules | No — validators live inside the JD schema class itself |
| Embedding builder | No — hardcoded JD field names in `build_canonical_embedding_text` |
| Persistence mapper | No — `JDService`/`JDRepository` are JD-only; no generic mapper interface |
| Stage tracking/telemetry | **Yes** — `document_type` param already flows through `StageExecutionService`, checkpoint tables |
| Skill normalization | **Yes** — generic string-list interface, no JD assumption |
| Retry policy | **Yes** — keyed by stage, not document type |
| Embedding model call | **Yes** — generic string-in/vector-out |
| Text cleaning | **Yes** — generic normalization |

**Pre-provisioned but unwired resume scaffolding** (confirms this was designed for, not just theoretically possible):
- `DocumentType.RESUME` enum value, DB enum `document_type_enum` includes `'RESUME'`.
- `Resume`, `ResumeParseAttempt`, `ResumeEmbedding`, `CandidateSkill` ORM models and their tables.
- `S3_RESUME_PREFIX` constant sitting next to `S3_JD_PREFIX` (`app/enums/constants.py:64-65`).
- `resume_id` nullable FK columns already present on `celery_task_log` and `dead_letter_queue`.
- None of this is currently referenced by any service, pipeline, task, or route — it's schema/enum-level groundwork only.

---

## 9. Resume-readiness gap report

| Current implementation | Why it is JD-specific | Minimal abstraction/configuration needed |
|---|---|---|
| `SYSTEM_PROMPT` constant, `app/prompts/jd_extraction_prompt.py` | Hardcoded JD framing and field instructions, imported directly by name | A resume-equivalent prompt module/constant; extraction service parameterized to accept a prompt instead of importing one constant |
| `JDExtractionResponse` / `JDExtractionGenerationSchema` | JD-only field set (skills/responsibilities/certifications/experience/education/employment/work_mode/location) | A parallel `ResumeExtractionResponse` schema; pipeline's JSON_VALIDATION stage parameterized by schema class rather than hardcoded reference |
| `GeminiExtractionService.extract_raw()` | Not JD-specific itself (just takes text), but always paired with the JD schema/prompt at call sites | Accept prompt + response_schema as parameters (mechanically already close — just needs call-site parameterization, not a rewrite) |
| `TextExtractionService.extract(..., source_format: JDSourceFormat)` | Parameter type is namespaced under `app/models/jd/` | A generic/shared source-format enum (or reuse as-is if semantically acceptable) |
| `EmbeddingService.build_canonical_embedding_text()` | Hardcoded JD field names/labels read off `JDExtractionResponse` | A resume-specific canonical-text builder function; `generate_embedding()` itself needs no change |
| `JDService.persist_processed_jd()`, `JDRepository` | Writes only to `job_descriptions`/`jd_embeddings`/`jd_skills`/`jd_unknown_skills` | A parallel `ResumeService`/`ResumeRepository` writing to the already-defined `resumes`/`resume_embeddings`/`candidate_skills` tables |
| `JDProcessingPipeline`, `JDProcessingContext` | Class names, hardcoded stage-method bodies, `document_type` default of `JD` | A parallel `ResumeProcessingPipeline`/`ResumeProcessingContext` — this is the pattern the code's own docstrings already prescribe, not a gap to "fix" |
| `process_jd_document` Celery task, `jd_routes.py` endpoints | JD-only task name/route prefix | A parallel `process_resume_document` task + `/resumes` routes |
| `EntityType`/`ActionType` enums (audit log) | No `RESUME`/`CANDIDATE_SKILL`/`RESUME_*` members | Enum migration adding resume-side audit entity/action types |
| `S3_JD_PREFIX` used in storage stage | JD-specific constant, but storage mechanism itself is generic | Select prefix constant based on document type (already have `S3_RESUME_PREFIX` defined, just unused) |
| Alembic migration graph gaps (missing revision files) | Pre-existing technical debt, unrelated to resumes but a blocker for further schema changes | Reconcile/repair migration history before layering resume migrations on top |
| `StageExecutionService`, `document_processing_stage_executions`, `CheckpointRepository`, `DocumentProcessingRepository`, `RetryDriver`/`retry_policy`, `SkillNormalizationService`, `SkillOntologyRepository` (generic ontology methods) | N/A — already generic | **No change needed** — reusable as-is |

---

## 10. Final verdict

**Already reusable as-is (no change needed):**
- `PreprocessingService.normalize()` (text cleaning)
- `SkillNormalizationService.normalize_skills()` and the shared `skill_ontology`/`unknown_skills`/`skill_suggestions` tables
- `EmbeddingService.generate_embedding()` (the model call itself)
- `StageExecutionService` + `document_processing_stage_executions` / `document_processing_checkpoints` tables + their repositories
- `RetryDriver` / `retry_policy` (stage-keyed, not document-type-keyed)
- The `DocumentType` enum and DB enum already include `RESUME`

**Needs only configuration/parameterization (small, mechanical):**
- Storage stage's S3 prefix selection (`S3_JD_PREFIX` vs. already-defined `S3_RESUME_PREFIX`)
- `TextExtractionService.extract()`'s source-format parameter type (generalize or reuse)
- `GeminiExtractionService` call sites (already accepts arbitrary text; just needs prompt/schema passed in instead of hardcoded imports)

**Tightly coupled to JD and must be abstracted/duplicated:**
- The extraction prompt (`jd_extraction_prompt.py`)
- The extraction/validation schema (`JDExtractionResponse`, `JDExtractionGenerationSchema`)
- The embedding canonical-text builder (`build_canonical_embedding_text`)
- The persistence layer (`JDService`, `JDRepository`, `job_descriptions`/`jd_embeddings`/`jd_skills` tables)
- The orchestrator and context (`JDProcessingPipeline`, `JDProcessingContext`) — though per the codebase's own documented design intent, this is expected to be **duplicated, not shared**, for the resume pipeline
- Entry points (routes, Celery task)
- Audit-log enums (`EntityType`, `ActionType`) missing resume members

**Minimum set of changes needed to support resumes** (mirroring, not modifying, the existing pipeline):
1. A resume extraction prompt + `ResumeExtractionResponse` schema (parallel to `jd_extraction_prompt.py` / `JDExtractionResponse`).
2. A resume canonical-text builder for embeddings (parallel to `build_canonical_embedding_text`).
3. A `ResumeService`/`ResumeRepository` persistence layer targeting the already-existing `resumes`/`resume_embeddings`/`candidate_skills` tables, plus a `create_candidate_skill`-equivalent method in (or beside) `SkillRepository`.
4. A `ResumeProcessingContext` + `ResumeProcessingPipeline` reusing `StageExecutionService`, `CheckpointRepository`, `DocumentProcessingRepository`, `RetryDriver`, `PreprocessingService`, and `SkillNormalizationService` directly.
5. A `process_resume_document` Celery task + `/resumes` routes, following the same synchronous-VALIDATION/STORAGE-then-async pattern as `jd_routes.py`.
6. Enum migration adding `RESUME`/`CANDIDATE_SKILL`/`RESUME_*` members to `EntityType`/`ActionType` for audit logging.
7. (Housekeeping, unrelated to resumes but blocking) resolve the gaps in the Alembic migration graph before adding resume-table migrations.

**Recommended order of changes** (no code yet, per your instruction):
1. Fix the Alembic migration graph gaps (pure risk-reduction, independent of resume work).
2. Define `ResumeExtractionResponse` schema + resume prompt (unblocks everything downstream — nothing else can be tested without extracted resume JSON).
3. Build `ResumeProcessingContext` + `ResumeProcessingPipeline` skeleton reusing the generic services (stage tracker, checkpoint/retry, preprocessing, skill normalization) — validates that the generic layer really is reusable end-to-end before investing in persistence.
4. Add the resume canonical-embedding-text builder.
5. Add `ResumeRepository`/`ResumeService` persistence targeting existing resume tables, plus the missing `candidate_skills` write path.
6. Add `process_resume_document` task + `/resumes` routes.
7. Extend `EntityType`/`ActionType` enums for resume audit events.
