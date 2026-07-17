# Resume Intake Epic (M05) — Implementation Log

**Module:** M05 – Resume Intake | Individual Resume Upload (Epic 1) + Bulk ZIP Upload (Epic 2)
**Scope:** Epic 1 (M05-E01) Phases 0–11, and Epic 2 (M05-E02) Phases B0–B9 of their respective implementation roadmaps
**Status:** All 12 Epic 1 phases and all 10 Epic 2 phases implemented. Both live-tested — Epic 1 end-to-end through a real upload → encryption → storage → pipeline-entry → Celery parse → status-poll cycle; Epic 2 through real ZIP upload → extraction → parse-first candidate creation → cap enforcement → retry/DLQ → cancellation → history/export → failure-mode cleanup, verified primarily at the service/task layer (see Epic 2's Cross-Cutting Issues — Redis was unavailable throughout this environment, so full HTTP round-trips could only be exercised for routing/schema shape, not live Celery dispatch).
**Companion document:** `docs/resume_intake_implementation_plan.md` (the original pre-implementation plan, including the Epic 2 schema-changes addendum)

## How to read this document

Each phase section is split into two halves, matching how the work was actually done:

- **Before implementation** — what was planned: objective, why it was needed, which files were expected to change, which existing components would be reused.
- **After implementation** — what actually happened: files touched, classes/methods/APIs added, verification performed, and any bugs or deviations discovered along the way (several phases surfaced real, pre-existing defects in code outside the phase's own scope — these are called out explicitly where they occurred).

A cross-cutting section after the phase-by-phase log covers bugs and environment issues that spanned multiple phases, plus a running list of what's still open.

---

# Epic 1 (M05-E01): Individual Resume Upload — Phases 0–11

## Constraints in effect throughout

- Database already exists — **no migrations were created**.
- Every phase uses **existing tables only**.
- Where a dependency wasn't fully built yet, seeded/mock data was used through the repository layer rather than blocking on it.
- A recurring, structural limitation surfaced repeatedly during live testing: **Postgres native enum types** (`audit_action_type_enum`, `audit_entity_type_enum`) do not automatically gain new values when the corresponding Python `enum.Enum` classes are extended — a matching `ALTER TYPE ... ADD VALUE` has to be run separately. This was hit and handled three times (Phases 0/7 and again in Phase 11).

---

## Phase 0 — Config & Enum Foundations

**Before:** Seed every constant value and audit vocabulary the rest of the epic would need — new `platform_config` rows, new `ActionType`/`EntityType` members, one `ACTIVE` `encryption_keys` row — before any service code was written.

**After:**
- **Modified:** `app/enums/constants.py` (added `RESUME_UPLOADED`, `CONSENT_RECORDED`, `UPLOAD_BLOCKED_ERASURE_REQUEST` to `ActionType`; `CANDIDATE`, `RESUME`, `CONSENT` to `EntityType`), `app/seeds/seed_platform_config.py` (added `RESUME_MAX_SIZE_MB`, `CONSENT_VERSION`, `JURISDICTION_CONSENT_CONFIG` — the latter a JSON string, since `platform_config.value` is a plain `String`, not `JSONB`)
- **Created:** `app/seeds/seed_encryption_key.py`
- **Finding:** confirmed directly against the live database that the new `ActionType`/`EntityType` values did not yet exist in the native Postgres enum types — flagged as a blocker to resolve before Phase 7, not resolved yet at this point.
- **Verification:** enum values importable in Python; seed scripts compile and (later) were run successfully against the real database.

---

## Phase 1 — Encryption Service Foundation

**Before:** Stand up PII encryption and MD5 dedup-hashing as a standalone capability — key resolution, Fernet encrypt/decrypt, hash generation — independent of Candidate/Resume code.

**After:**
- **Created:** `app/repositories/encryption_key_repository.py`, `app/core/encryption.py`, `app/core/encryption_service.py`
- **Classes:** `EncryptionKeyRepository`, `EncryptionService`, `EncryptionUnavailableError` (later renamed to `EncryptionUnavailableException` in Phase 10), `DecryptionError`
- **Methods:** `get_active_by_purpose`, `get_rotating_by_purpose`, `get_by_id`, `resolve_key_material`, `encrypt`, `decrypt`, `generate_hash`
- **Design note:** key material is resolved from `.env` by `key_alias`, never from the database, per the spec's explicit security requirement.
- **Verification:** hash normalization confirmed directly (`"  Test@Example.COM "` and `"test@example.com"` produce identical MD5 output). Full DB-backed encrypt/decrypt round-trip was verified later, live, once Phase 0's key row actually existed in the real database.

---

## Phase 2 — Consent Repository & Service

**Before:** Make consent capture callable — record a consent event, validate a candidate's consent adequacy against jurisdiction rules — reusing Phase 1's encryption service and Phase 0's seeded config.

**After:**
- **Created:** `app/repositories/consent_repository.py`, `app/services/compliance/__init__.py`, `app/services/compliance/consent_service.py`
- **Classes:** `ConsentRepository`, `ConsentService`
- **Methods:** `create` (insert-only), `get_latest_by_candidate`, `record_consent`, `is_adequate`, plus private helpers for version/jurisdiction lookups
- **Design note:** `CandidateConsent` is treated as insert-only — no update/delete methods exposed, matching the DB's intended immutable-audit-trail design.
- **Verification:** the version-comparison helper (`_version_at_least`) confirmed directly (`1.0` vs `1.0` → true, `1.1` vs `1.0` → true, `0.9` vs `1.0` → false).

---

## Phase 3 — Candidate Repository & Service

**Before:** Deliver the atomic "encrypt PII, hash for dedup, create-or-reuse candidate, record consent" operation — the convergence point of Phases 1 and 2, and the single hardest blocker identified in the earlier dependency-verification pass.

**After:**
- **Created:** `app/exceptions/candidate_exceptions.py`, `app/repositories/candidate_repository.py`, `app/services/resume/__init__.py`, `app/services/resume/candidate_service.py`
- **Classes:** `CandidateErasureBlockedException`, `CandidateRepository`, `CandidateService`
- **Methods:** `get_by_email_hash`, `create` (race-safe via SAVEPOINT), `update_erasure_fields`, `get_or_create`, `_build_encrypted_candidate`, `_raise_if_erasure_blocked`
- **Design note:** the `email_hash` unique-constraint race (two concurrent uploads of a brand-new candidate) is handled with the exact same SAVEPOINT idiom already established by `SkillRepository.upsert_unknown_skill` — reused, not reinvented.
- **Verification:** confirmed logically consistent at implementation time; full live confirmation (encrypted columns populated, plaintext never persisted, dedup by email, erasure-block enforcement) came later during real upload testing.

---

## Phase 4 — File Validation Service

**Before:** Magic-byte format detection, size-limit enforcement, and per-format integrity/corruption checks — pure, DB-independent (aside from one config read).

**After:**
- **Modified:** `requirements.txt` — added `filetype==1.2.0`
- **Created:** `app/services/resume/file_validation_service.py`
- **Classes:** `FileValidationService`, `FileValidationResult`, `UnsupportedFileFormatError`/`FileSizeExceededError`/`CorruptFileError` (later renamed to `...Exception` in Phase 10)
- **Deliberate deviation:** used `filetype` (pure Python) instead of the roadmap's suggested `python-magic`, which needs a native `libmagic` binary — a known pain point on Windows. Verified against real generated fixture files before committing to the choice.
- **Verification (live, against real generated fixtures):** valid DOCX/PNG correctly detected; extension-mismatch correctly rejected; garbage content correctly rejected; corrupt PDF header correctly rejected with pdfium's real error; size-limit correctly enforced against an 11 MB padded file.

---

## Phase 5 — Resume Repository & Upload Service (Sync Leg)

**Before:** Wire file validation + storage + candidate creation into one orchestrated flow, stopping short of Celery so the phase is independently testable.

**After:**
- **Created:** `app/repositories/resume_repository.py`, `app/services/resume/resume_service.py`
- **Classes:** `ResumeRepository`, `ResumeService`
- **Methods:** `create`, `get_by_id`, `get_active_by_candidate`, `record_parse_attempt`, `upload`, `_build_object_path`, `_hash_file_bytes`
- **Design note:** object storage path and bucket-naming convention copied directly from `JDService`'s existing pattern (`org_{org_id}/resume/{uuid4()}.{ext}`), not invented fresh.
- **Verification:** pure helpers (MD5 hash, object-path construction) confirmed directly; full upload-to-storage round trip confirmed later during live testing (see Cross-Cutting Issues — bucket-name mismatch).

---

## Phase 6 — Campaign-Candidate Pipeline Hardening

**Before:** Fix two known correctness gaps in the existing, already-shipping `CampaignCandidateService`: a placeholder (non-deterministic) idempotency key, and a missing `campaign_candidate_stage_history` insert — plus add row-locking for the candidate-cap race condition.

**After:**
- **Modified:** `app/repositories/CampaignRepository.py` (added `get_by_id_for_update`), `app/repositories/campaign_candidate_repository.py` (added `get_by_idempotency_key`, `create_idempotent`, `create_stage_history`), `app/services/campaign/campaign_candidate_service.py` (locked campaign read, deterministic SHA-256 idempotency key, stage-history insert)
- **Bug found and fixed:** `idempotency_key` was previously `str(uuid.uuid4())` — a random value with an explicit `# temporary placeholder` comment in the original code, meaning retries could never actually be detected as duplicates. Replaced with a deterministic SHA-256 hash of `campaign_id:candidate_id:resume_id`.
- **Verification:** deterministic-hash property confirmed directly (same inputs → same key; different resume → different key; 64-char SHA-256 hex). The row-lock fix itself required a follow-up correction — see Cross-Cutting Issues.

---

## Phase 7 — Upload Orchestration API

**Before:** Expose the first real HTTP endpoint (`POST /airs/resumes`), chaining Phases 3–6 together synchronously, deliberately without a Celery enqueue yet.

**After:**
- **Created:** `app/schemas/resume/__init__.py`, `app/schemas/resume/request.py`, `app/schemas/resume/response.py`, `app/dependencies/resume.py`, `app/services/resume/resume_intake_service.py`, `app/api/routes/resume_routes.py`
- **Modified:** `app/main.py` (router registration)
- **Classes:** `ResumeUploadRequest`, `ResumeUploadAcceptedResponse`, `ResumeIntakeService`
- **APIs added:** `POST /airs/resumes`
- **Pre-existing bug found and fixed (outside this phase's own scope, but blocking it):** `CampaignCandidateResponse.pipeline_stage` was typed against the wrong `PipelineStage` enum (`app.enums.constants` instead of `app.models.pipeline`) — meaning `CampaignCandidateService.create_campaign_candidate` had been raising a `ValidationError` on every successful candidate creation, silently, since before this epic began. Confirmed broken via direct test, then confirmed fixed via the same test. Fixed in `app/schemas/campaign/campaign_candidate_schema.py`.
- **Verification:** route registration confirmed in the live OpenAPI schema; auth gating confirmed (401 without a token); all 5 `ResumeUploadRequest` validation scenarios confirmed directly.
- **Operational decision made this phase:** with the user's explicit approval, ran `ALTER TYPE audit_action_type_enum ADD VALUE ...` / `ALTER TYPE audit_entity_type_enum ADD VALUE ...` for the six enum values Phase 0 had added in Python only — closing that flagged blocker. No Alembic migration file was created; this matched the precedent already set by a prior migration in this repo for `CAMPAIGN_RESUMED`.

---

## Phase 8 — Resume Processing Pipeline & `RESUME_PARSE` Celery Task

**Before:** Add the async leg — text extraction, cleaning, Gemini-based structured extraction, persistence — and wire the enqueue call into `ResumeIntakeService`.

**After:**
- **Created:** `app/schemas/ai/resume_extraction_response.py`, `app/prompts/resume_extraction_prompt.py`, `app/services/extractions/gemini_resume_extraction_service.py`, `app/services/resume/resume_processing_context.py`, `app/services/resume/resume_processing_pipeline.py`, `app/tasks/resume_processing_tasks.py`
- **Modified:** `app/core/celery_app.py` (task registration), `app/services/document_processing/text_extraction_service.py` (added a `FileFormat`-dispatched extraction path alongside the existing JD-facing one, unchanged), `app/services/resume/resume_intake_service.py` (the actual enqueue call), `app/schemas/resume/response.py` + `app/api/routes/resume_routes.py` (added `task_id` to the upload response)
- **Classes:** `ResumeExtractionResponse`, `GeminiResumeExtractionService`, `ResumeProcessingContext`, `ResumeProcessingPipeline`
- **Celery task:** `resume.process_document`
- **Pre-existing bug found and fixed (outside this phase's own scope):** `settings.gemini_model` was referenced by the existing `GeminiExtractionService` (JD's) but never declared on `Settings` — meaning **JD's own AI-extraction stage had been broken all along**, independent of anything in this epic. `.env` already had a real `GEMINI_MODEL` value sitting unused because pydantic-settings silently drops undeclared env vars. Fixed by adding the field to `app/core/config.py`.
- **Scope decision:** the pipeline deliberately stops before skill normalization/embedding generation (a later epic's responsibility) and deliberately cannot parse PNG/JPEG resumes (no OCR library exists) — image-format resumes are detected up front and marked `parse_status=FAILED` with `error_code=OCR_NOT_SUPPORTED`, a clean, designed failure rather than a crash.
- **Verification:** full pipeline control flow tested against in-memory stubs for both the DOCX happy path (all 5 stages run, `parsed_json` populated, `parse_status=PARSED`) and the PNG OCR-unsupported path (stages skipped, clean `FAILED` outcome) — both confirmed correct. JD's existing `TextExtractionService.extract()` confirmed unaffected by the new method added alongside it.

---

## Phase 9 — Processing Status Polling Endpoint

**Before:** `GET /airs/resumes/processing-status/{task_id}`, combining `CeleryTaskLog` and `DocumentProcessingStageExecution` into one response, closing the loop opened by Phase 7/8.

**After:**
- **Created:** `app/services/resume/resume_processing_status_service.py`
- **Modified:** `app/schemas/resume/response.py` (`StageProgress`, `ResumeProcessingStatusResponse`), `app/dependencies/resume.py`, `app/api/routes/resume_routes.py`
- **Classes:** `StageProgress`, `ResumeProcessingStatusResponse`, `ResumeProcessingStatusService`
- **APIs added:** `GET /airs/resumes/processing-status/{task_id}`
- **Design note:** `StageProgress` was deliberately duplicated (not imported) from the JD schemas' identical-shaped class, keeping `schemas/resume` from depending on `schemas/jd` — consistent with how `ResumeProcessingContext` was kept separate from `JDProcessingContext` in Phase 8.
- **Verification:** route registration and auth-gating confirmed; **404 handling confirmed against two real, actually-stuck task IDs from live testing** — which is what first surfaced that no Celery worker had ever successfully processed either task (see Cross-Cutting Issues).

---

## Cross-Cutting Issues Discovered During Live Testing

These weren't scoped to a single phase — they surfaced once the system was exercised end-to-end with a real browser/Postman client, a real database, real Supabase storage, a real Celery worker, and the real Gemini API.

### 1. Connection-pool exhaustion (recurring, environmental)
`FATAL: remaining connection slots are reserved for roles with the SUPERUSER attribute` — hit repeatedly throughout the session. Root cause: a small Aiven Postgres connection limit, contended by `uvicorn --reload` (two processes), the Celery worker, and ad-hoc verification scripts run during development. Not a code defect; noted as worth revisiting (`pool_size`/`max_overflow` in `app/db/database.py`, or an Aiven plan check) if it recurs during normal use rather than heavy concurrent debugging.

### 2. `SELECT ... FOR UPDATE` + `lazy="joined"` outer join (PostgreSQL rejection)
`CampaignRepository.get_by_id_for_update()` (built in Phase 6) inherited a mapper-level `lazy="joined"` default from `HiringCampaign.job_description`, producing `LEFT OUTER JOIN job_descriptions ... FOR UPDATE` — which PostgreSQL rejects outright (`FeatureNotSupported: FOR UPDATE cannot be applied to the nullable side of an outer join`). Root-caused via a full explain-before-fix pass (SQLAlchemy config vs. PostgreSQL locking semantics), fixed with a per-query `lazyload(HiringCampaign.job_description)` override — zero blast radius on any other caller of `HiringCampaign`. Confirmed via compiled-SQL inspection and a live locked read.

### 3. Hardcoded `"SYSTEM"` audit actor (pre-existing, foreign-key violation)
`CampaignCandidateService` (pre-existing code, already flagged in its own comments as a placeholder) hardcoded `actor_id="SYSTEM"` in three audit-log calls. `audit_log.actor_id` has a foreign-key constraint to `users.id`, and no such row exists — every write failed with `ForeignKeyViolation`. Fixed by threading a real `actor_id`/`actor_role` through `create_campaign_candidate`/`delete_campaign_candidate`, and — since the pre-existing `POST /campaign-candidates` route had **no identity resolution at all** — adding `Depends(get_current_user)` there too, closing the same latent defect everywhere it existed, not just on the path that happened to get tested first.

### 4. Supabase bucket-name mismatch
`ResumeService`/`ResumeProcessingPipeline` assumed a bucket named `airs-resumes` (hyphen, mirroring the JD module's `airs-job-descriptions`). The actual pre-existing bucket was `airs_resumes` (underscore). Fixed in both files once the real bucket list was inspected directly.

### 5. Missing encryption key material in the real environment
Phase 0's `seed_encryption_key.py` had never been run against the real database, and no real Fernet key existed in `.env` yet. Fixed by running the seed script and generating/appending a real key to `.env` as `ENCRYPTION_KEY_CANDIDATE_PII_V1`.

### 6. Celery worker incompatible with Windows (billiard/prefork crash)
The first attempt to run a Celery worker used the default pool (`prefork`, built on Unix `fork()`), which crashed repeatedly on Windows with `PermissionError: [WinError 5]` / `OSError: [WinError 6]` from the `billiard` library's inter-process primitives. The worker had already grabbed two real tasks off the queue before crashing, leaving them stuck in Redis's "unacknowledged" state. Fixed by restarting with `--pool=solo`; the two stuck messages were then manually restored from Redis's `unacked` hash back onto the `celery` queue (replicating Kombu's own internal restore mechanism exactly), and both were successfully picked up and processed by the now-stable worker.

### 7. Gemini transient `503 UNAVAILABLE`
Observed twice, consistently, on the `AI_EXTRACTION` stage — an external, transient Gemini capacity issue, not a defect in this codebase. `TEXT_EXTRACTION`/`TEXT_CLEANING` succeeded reliably both times, and the affected resume correctly remained at `parse_status=PENDING` with nothing partially written, confirming the pipeline's atomicity guarantee held under a real failure. A manual re-enqueue of the same resume (bypassing the upload/campaign-candidate flow entirely, since re-uploading would hit the duplicate-candidate check) was used to retry it.

### 8. Postgres native enum vs. Python enum drift
Covered under Phase 0/7 above — worth restating as a standing pattern: any future addition to `ActionType`/`EntityType` needs a matching `ALTER TYPE` before it can actually be written, not just a Python-side change.

---

## Phase 10 — Error Handling, Exception Types & Retry Safety

**Before:** Turn every remaining upload-failure scenario into a specific, distinguishable HTTP response instead of a generic 500.

**After:**
- **Created:** `app/exceptions/resume_exceptions.py`
- **Modified:** `app/services/resume/file_validation_service.py`, `app/core/encryption_service.py` (both: swapped locally-defined plain exceptions for the new, properly HTTP-aware ones), `app/exception_handler/handlers.py`, `app/main.py`
- **Classes:** `ResumeException` (base, mirrors `CampaignException`'s shape), `UnsupportedFileFormatException`, `FileSizeExceededException`, `CorruptFileException`, `EncryptionUnavailableException`
- **Additional fix folded in:** two **pre-existing, never-registered** exception types were also wired up here — `StorageException` (shared with the JD module; this is the exact class of bug behind Cross-Cutting Issue #4's raw 500) and `CandidateErasureBlockedException` (Phase 3, also unregistered until now).
- **Coverage confirmed against the epic's original 8 upload-failure scenarios:** all 8 now produce clean, typed responses — 3 already worked via `CampaignException` (paused/closed, cap reached, duplicate candidate), and this phase closed the remaining 5 (format, size, corruption, encryption-unavailable, storage-unavailable) plus the bonus erasure-block case.
- **Verification:** handler registration confirmed for all three new/newly-wired exception families; all 6 exception instances produced the correct status code and clean body when invoked directly, including confirming `StorageException`'s internal detail (bucket name) is deliberately **not** leaked to the client; regression-checked that `FileValidationService` still raises correctly under the renamed classes; a static grep confirmed zero remaining references to any old exception class name.

---

## Phase 11 — Circuit-Breaker Tracking (Stretch)

**Before:** Start populating the already-existing, never-used `circuit_breaker_state` table on repeated storage/encryption failures — no email alerting, since that infrastructure doesn't exist.

**After:**
- **Created:** `app/repositories/circuit_breaker_repository.py`
- **Modified:** `app/enums/constants.py` (`ActionType.CIRCUIT_BREAKER_OPENED`, `EntityType.CIRCUIT_BREAKER` — same DB-enum caveat as before, not yet applied to the live database), `app/services/resume/resume_service.py` (wrapped the storage call and candidate-creation call in failure tracking), `app/dependencies/resume.py`
- **Classes:** `CircuitBreakerRepository`
- **Design note:** service names used are `SUPABASE_STORAGE` and `ENCRYPTION_SERVICE` — deliberately not the epic's literal `MINIO`/`EMBEDDING_SERVICE` wording, since neither matches this system's actual infrastructure. The failure-tracking bookkeeping is wrapped so that any error in it is swallowed and logged, never masking the real exception it's reacting to.
- **Verification:** full app import/boot confirmed (proves the new DI chain wires correctly); a live DB round-trip test of the `CLOSED → OPEN` transition was attempted but blocked by the same connection-pool exhaustion described above — left as a script for later confirmation rather than claimed as verified.

---

## Summary Table — All 12 Phases

| Phase | Focus | New HTTP surface | Bugs found outside own scope |
|---|---|---|---|
| 0 | Config & enum foundations | No | — |
| 1 | Encryption service | No | — |
| 2 | Consent repository & service | No | — |
| 3 | Candidate repository & service | No | — |
| 4 | File validation service | No | — |
| 5 | Resume repository & upload service | No | — |
| 6 | Campaign-candidate pipeline hardening | No | Random (non-deterministic) idempotency key in existing code |
| 7 | Upload orchestration API | `POST /resumes` | `CampaignCandidateResponse` enum-import bug (pre-existing) |
| 8 | Resume processing pipeline + Celery | No (extends existing endpoint) | Missing `gemini_model` setting (broke JD's AI extraction too) |
| 9 | Processing status polling endpoint | `GET /resumes/processing-status/{task_id}` | — |
| 10 | Error handling & exception types | No (hardens existing endpoint) | Unregistered `StorageException`/`CandidateErasureBlockedException` |
| 11 | Circuit-breaker tracking (stretch) | No | — |

---

## Known Gaps / Follow-Up Work

- **Email notification/alerting module** — referenced across several M16 compliance stories and Phase 11's own circuit-breaker design; no library, service, or template system exists yet.
- **OCR for image-format resumes** — PNG/JPEG upload and validation work fully; parsing does not. Currently a clean, designed failure (`OCR_NOT_SUPPORTED`), not a silent gap.
- **Reprocess/retry API endpoint** — discussed as a natural follow-up but not built; retries were demonstrated via a direct Celery re-enqueue script instead of a dedicated endpoint.
- **`ALTER TYPE` for `CIRCUIT_BREAKER_OPENED` / `CIRCUIT_BREAKER`** — added to Python enums in Phase 11 but not yet applied to the live database (unlike the Phase 0/7 values, which were approved and applied).
- **M16 Compliance stories beyond consent-capture** — dashboard, coverage reporting, consent withdrawal, jurisdiction-config admin UI — all out of scope for this epic slice.
- **Resume skill extraction & embedding generation** — deliberately deferred; the processing pipeline stops at structured `parsed_json`, matching the architecture mapping's "modules dependent on future work."
- **Bulk ZIP upload** — was a separate epic at the time this note was written; **now implemented in full** — see Epic 2 (M05-E02), Phases B0–B9, below.
- **Connection-pool sizing** — worth a deliberate look if exhaustion recurs outside of heavy concurrent debugging sessions.

---

*(Epic 1's own Known Gaps above are left as originally written, aside from the bulk-upload note; Epic 2 introduces its own gaps, listed at the very end of this document.)*

---

# Epic 2 (M05-E02): Bulk ZIP Upload — Phases B0–B9

## Constraints in effect throughout

- Unlike Epic 1, the **"no migrations" constraint was explicitly relaxed for this epic**, with the user's own sign-off (see the schema-changes addendum in `resume_intake_implementation_plan.md`). Every schema change below went through a real Alembic migration, not an ad-hoc `ALTER TYPE`.
- **"Parse-first" architecture**: unlike an individual upload (where the candidate's name/email come from the upload form before parsing ever runs), a bulk ZIP has no such form data per file — text/AI extraction has to run *first* to learn who the candidate even is, and only then are `Candidate`/`Resume`/`CampaignCandidate` rows created. This one decision shaped B3's staging table, B4's task ordering, and B6's checkpoint contents.
- No real Celery-level task cancellation (`revoke()`) exists anywhere in this codebase (confirmed by research before building B7) — "cancellation" here means a cooperative DB status flip plus an early-exit check in the per-file task, not killing an in-flight worker process.
- **Redis was unavailable throughout this development environment.** Every phase's Celery task was verified by calling it directly (`task.run(...)`) or invoking the underlying service methods, never through a real `apply_async` + worker round-trip. This is the single biggest reason a routing regression (see Cross-Cutting Issue #6) went unnoticed for six phases.

---

## Phase B0 — Schema & Config Foundations

**Before:** Add every column/enum-value/config-row the rest of the epic would need, this time via real Alembic migrations rather than Epic 1's ad-hoc `ALTER TYPE` approach.

**After:**
- **Created:** `alembic/versions/a3f9c72e1b6d_bulk_zip_upload_schema.py` — adds `resumes.bulk_upload_job_id`, `celery_task_log.bulk_upload_job_id`, `bulk_upload_jobs.consent_confirmed`, plus (in the *same* migration, unlike Epic 1) three new audit enum values: `ActionType.BULK_UPLOAD_CANCELLED`, `ActionType.BULK_UPLOAD_HISTORY_EXPORTED`, `EntityType.BULK_UPLOAD_JOB`.
- **Modified:** `app/models/candidates.py`, `app/models/async_tasks.py`, `app/enums/constants.py`, `app/seeds/seed_platform_config.py` (added `ZIP_MAX_SIZE_MB=500`, `MAX_FILES_PER_ZIP=200`, the latter with no epic-specified number — my own chosen default).
- **Bug found and fixed (pre-existing, unrelated to this epic):** the Alembic chain was already broken before this phase started — migration `d5c1a0b2e3f4`'s `down_revision` pointed at a nonexistent revision, and the live DB's `alembic_version` had drifted to a third, also-nonexistent value. Verified the actual live schema already matched what that migration should have produced (pure bookkeeping drift, no missing DDL), then repaired the file's `down_revision` and used `alembic stamp --purge` to reconcile tracking without running any DDL.
- **Verification:** migration applied live; all 3 columns and 3 enum values confirmed directly against the real database; seeded config confirmed present.

---

## Phase B1 — ZIP Validation & Job Repository

**Before:** `ZipValidationService` (extension/magic-byte/size checks, mirroring `FileValidationService`'s shape) and `BulkUploadJobRepository` (atomic SQL-level counters, mirroring the project's established "never read-modify-write a counter" rule).

**After:**
- **Created:** `app/exceptions/bulk_upload_exceptions.py` (`UnsupportedArchiveFormatException`, `ZipSizeExceededException`), `app/services/bulk_upload/zip_validation_service.py`, `app/repositories/bulk_upload_job_repository.py`
- **Classes:** `ZipValidationService`, `BulkUploadJobRepository`
- **Verification (live):** a real ZIP fixture correctly detected/passed; a real DB round-trip test (real campaign, real `uploaded_by`) confirmed atomic counter increments and cleanup.

---

## Phase B2 — Bulk Upload Intake API

**Before:** `POST /airs/bulk-uploads` — validate the ZIP, store it, create the `bulk_upload_jobs` row at `PENDING`. Deliberately no Celery enqueue yet (mirrors Epic 1's Phase 7/8 split).

**After:**
- **Created:** `alembic/versions/c8d4f1a6e9b2_bulk_upload_zip_path.py` (adds `bulk_upload_jobs.zip_storage_path`), `app/schemas/bulk_upload/__init__.py`/`request.py`/`response.py`, `app/services/bulk_upload/bulk_upload_service.py`, `app/dependencies/bulk_upload.py`, `app/api/routes/bulk_upload_routes.py`
- **Classes:** `BulkUploadRequest`, `BulkUploadAcceptedResponse`, `BulkUploadService`
- **Schema gap found mid-design (approved via user sign-off, not assumed):** `bulk_upload_jobs` had nowhere to durably store the ZIP's own storage path — needed for crash recovery, mirroring how a resume's `file_path` had already been relied on once in Epic 1 to manually recover a stuck task. Closed with the migration above rather than working around it.
- **Verification (live):** happy path (real 2-file ZIP → job created, storage round-trip byte-verified); rejected paths (non-ZIP content, paused campaign) both confirmed before any DB/storage write.

---

## Phase B3 — `BULK_EXTRACT` Celery Task

**Before:** Download the stored ZIP, unpack its real entries, stage each as its own storage object, and enqueue Phase B4's per-file task for each.

**After:**
- **Created:** `alembic/versions/e1b7c4a9d2f6_bulk_upload_job_files.py` (new `bulk_upload_job_files` table + `bulk_upload_file_status_enum`), `app/repositories/bulk_upload_job_file_repository.py`, `app/tasks/bulk_upload_tasks.py` (`extract_bulk_upload_zip`)
- **Classes:** `BulkUploadFileStatus`, `BulkUploadJobFile`, `BulkUploadJobFileRepository`
- **Schema gap found mid-design (approved via user sign-off):** because of the parse-first design, no `Resume` row can exist per file until *after* its AI extraction succeeds — so extracted files had nowhere to live as domain rows while queued. A new dedicated table was chosen over overloading `CeleryTaskLog` (which exists for task-execution logging, not file/domain state).
- **Migration hiccup (self-caused, fixed same phase):** the first migration attempt explicitly created the enum type and then let `create_table` create it *again* implicitly — Postgres correctly rejected the duplicate `CREATE TYPE`. Fixed by letting `create_table` create the enum exactly once.
- **Verification (live):** a 5-entry ZIP (3 real files including one nested path, plus `__MACOSX/` and `.DS_Store` junk) correctly staged only the 3 real files, byte-verified after download; a corrupted-archive job correctly moved to `FAILED` with the real `BadZipFile` message recorded on both the job and its `celery_task_log` row.

---

## Phase B4 — `BULK_RESUME_PARSE` (Parse-First Per-File Task)

**Before:** The per-file task itself — text extraction → AI extraction (Gemini) → *then* `Candidate`/`Resume`/`CampaignCandidate` creation, inverting Epic 1's order because no identity exists yet.

**After:**
- **Modified:** `app/tasks/bulk_upload_tasks.py` (`parse_bulk_upload_file`), `app/repositories/bulk_upload_job_file_repository.py` (`get_by_id`, `update_status`)
- **Design note:** reused Epic 1's `FileValidationService`, `TextExtractionService`, `PreprocessingService`, `GeminiResumeExtractionService`, `CandidateService.get_or_create`, `ResumeRepository`, and — critically — the *existing* `CampaignCandidateService.create_campaign_candidate`, whose already-built "candidate already exists in this campaign" check is what gives bulk uploads duplicate-detection for free, without writing any new duplicate logic.
- **Bug found and fixed (in this phase's own new code):** `AuditRepository` has no `.commit()` method — a stray `audit_repo.commit()` call meant every otherwise-successful parse was quietly recorded as a task failure (though the DB writes still landed once a later `job_repo.commit()` on the same shared session ran). Fixed by removing the call, matching the established pattern where any repo sharing the session can commit the rest.
- **Job-finalization logic added:** `_maybe_finalize_job` — once every staged file resolves (processed + failed + duplicate == total), the job moves to `COMPLETED`/`PARTIAL_FAILURE`/`FAILED`.
- **Verification (live, three separate real runs):** (1) a genuine transient Gemini `503 UNAVAILABLE` was hit twice in a row — confirmed the failure path recorded it correctly without side effects; (2) with Gemini's response stubbed (to isolate this phase's own logic from external flakiness), the full success path was confirmed — `Candidate` encrypted, `Resume` `PARSED`, `CampaignCandidate` `UPLOADED`, audit logged, job `COMPLETED`; (3) a second file resolving to the same candidate/campaign correctly counted as a **duplicate**, not a failure.

---

## Phase B5 — `MAX_FILES_PER_ZIP` Cap Enforcement

**Before:** Enforce the cap seeded back in B0 but never used until now — reject the whole job outright (no partial processing) if a ZIP contains more real files than allowed, checked *before* any file is uploaded to storage.

**After:**
- **Modified:** `app/exceptions/bulk_upload_exceptions.py` (`MaxFilesExceededException`), `app/services/bulk_upload/zip_validation_service.py` (`validate_file_count`), `app/tasks/bulk_upload_tasks.py` (`extract_bulk_upload_zip` restructured into an enumerate-then-check-then-stage two-pass flow)
- **Verification (live):** a real 201-file ZIP (cap is 200) was rejected with zero storage uploads and zero `bulk_upload_job_files` rows created; a regression check confirmed a normal 2-file ZIP still staged correctly afterward.

---

## Phase B6 — Partial-Failure Handling + Dead Letter Queue

**Before:** Retry-with-backoff and a durable Dead Letter Queue for the genuinely transient steps (storage download, text extraction/cleaning, AI extraction) — reusing whatever this codebase already had, rather than inventing bulk-specific retry logic.

**After:**
- **Discovery:** this exact machinery already existed, fully built for the JD pipeline and unused anywhere else — `RetryDriver`, `error_classifier.classify()`, `StageFailureLogRepository`, `CheckpointRepository`, `DeadLetterQueueRepository`, `retry_policy` — plus `DocumentType.RESUME` and generic `ProcessingStage` names clearly pre-positioned for exactly this reuse.
- **Modified:** `app/services/document_processing/retry_driver.py`, `app/tasks/jd_processing_tasks.py`, `app/tasks/bulk_upload_tasks.py` (`parse_bulk_upload_file` → `bind=True`, wraps STORAGE/TEXT_EXTRACTION/TEXT_CLEANING/AI_EXTRACTION through the shared retry machinery; deterministic per-file outcomes — bad format, no identifiable candidate, duplicate — stay exactly as B4 built them, unwrapped)
- **Bug found and fixed (in shared, pre-existing JD infrastructure):** `RetryDriver.handle_failure` hardcoded `task_type="JD_DOCUMENT_PROCESSING"` when writing a `DeadLetterQueue` row — every bulk-upload DLQ entry would have been mislabeled as a JD failure. Fixed by adding a `task_type` constructor parameter (JD's own call site updated to keep passing its literal string, so its behavior is unchanged).
- **Bug found and fixed (a second, more serious pre-existing regression, unrelated to this phase's own work):** `EntityType` was missing `CANDIDATE`, `RESUME`, `CONSENT`, and `BULK_UPLOAD_JOB` — even though the live Postgres `audit_entity_type_enum` still had all of them. This silently broke Phase B4's own audit logging (and very likely Epic 1's individual-resume audit logging too) the moment `EntityType.RESUME` was actually referenced. Root cause: parallel, unrelated skill-ontology work had edited the same enum class and apparently dropped these members. Restored them — a pure Python-side fix, zero DB changes needed.
- **Structural fix:** flattened `parse_bulk_upload_file`'s try/except from nested to a single flat try with sibling `except` clauses — a nested structure would have let Celery's internal `Retry` signal get caught a second time by an outer catch-all, incorrectly marking a scheduled retry as a hard failure.
- **Verification (live, all three outcomes):** success (unchanged from B4); a forced `PERMANENT`-classified failure correctly wrote a `stage_failure_logs` row, a `dead_letter_queue` row with the *correct* `task_type`, kept its checkpoint (matching the JD precedent of preserving replay context), marked the file/job `FAILED`, and re-raised so Celery itself also sees the task as failed; a forced `TRANSIENT`-classified failure below its retry policy's max attempts left the job/file completely untouched (still `PROCESSING`/`QUEUED`) and only moved the task log to `RETRY`.

---

## Phase B7 — Cancellation

**Before:** `POST /bulk-uploads/{id}/cancel` — researched the existing campaign-pause feature first and deliberately mirrored its exact shape (a bulk DB status flip + audit log, in-flight work left to finish naturally), since no real Celery-level task revocation exists anywhere in this codebase.

**After:**
- **Created:** `alembic/versions/f2c9b8e4a1d3_bulk_upload_cancellation.py` (adds `BulkUploadStatus.CANCELLED`, and `BulkUploadFileStatus.RUNNING`/`CANCELLED`)
- **Modified:** `app/exceptions/bulk_upload_exceptions.py` (`BulkUploadJobNotFoundException`, `BulkUploadJobNotCancellableException`), `app/repositories/bulk_upload_job_file_repository.py` (`try_start_processing`, `cancel_queued_files`), `app/services/bulk_upload/bulk_upload_service.py` (`cancel_job`), `app/services/celery_task_log_service.py` (`mark_paused`, reusing the existing `TaskStatus.PAUSED` — whose own comment already called it "soft-cancelled"), `app/tasks/bulk_upload_tasks.py`
- **Race condition found and fixed (during this phase's own testing, before it could ship):** `bulk_upload_job_files` had no state analogous to `CeleryTaskLog.RUNNING` — the exact state that already protects campaign-pause's in-flight work from being touched. Without it, a file whose task was genuinely mid-flight was still `QUEUED` in the database and could be scooped up by the bulk-cancel `UPDATE`, then have its status silently reverted back to `PROCESSED`/`FAILED` once the task finished. Fixed with an atomic conditional claim (`try_start_processing`: `UPDATE ... WHERE status='QUEUED'` → `RUNNING`, checked via row count) plus the new `RUNNING` enum value — closing the race properly rather than accepting it.
- **Related fix:** `_maybe_finalize_job` now only acts while the job is still `PROCESSING`, so a straggler file that finishes normally *after* its job was already cancelled can't flip a `CANCELLED` job back to a computed terminal status.
- **Alembic multi-head issue found (pre-existing, not fixed):** applying this migration required resolving three divergent, never-merged Alembic heads (this epic's own chain, plus two from parallel JD/skill-ontology work). One of those other chains has its own pre-existing schema-drift bug (a column that already exists). Rather than fix someone else's broken, unrelated migration, the DDL for this phase was applied directly and `alembic stamp`'d — the multi-head situation itself was left exactly as found, flagged for whoever owns the other chains.
- **Verification (live):** cancel on a job with 1 already-processed + 2 queued files correctly cancelled only the 2 queued ones; re-cancelling and cancelling a nonexistent job both correctly rejected (409/404); the atomic-claim race fix directly verified — a file claimed `RUNNING` was excluded from the bulk cancel, a second claim attempt on it correctly failed, and it finished normally at `PROCESSED`, completely unaffected by the other (genuinely queued) file's cancellation.

---

## Phase B8 — Bulk Upload History

**Before:** List/detail/export endpoints for past bulk uploads, mirroring the JD module's existing pagination shape (`total`/`page`/`size`/`items`) and its xlsx-export convention (a `StreamingResponse` built via a static `ExcelExport` method, audit-logged with a nil-UUID sentinel entity id for list-level exports).

**After:**
- **Modified:** `app/repositories/bulk_upload_job_repository.py` (`list_by_campaign`, `count_by_campaign`, `get_all_by_campaign`), `app/services/bulk_upload/bulk_upload_service.py` (`get_job_detail`, `list_history`, `export_history`), `app/schemas/bulk_upload/response.py`, `app/utils/excel_export.py` (`export_bulk_upload_history`), `app/api/routes/bulk_upload_routes.py` (list/export/detail routes — `export` registered *before* the `{id}` route, exactly mirroring how `jd_routes.py` avoids "export" being swallowed as a path parameter)
- **Critical bug found and fixed (pre-existing, epic-wide impact, not scoped to this phase):** `app/main.py` imported `resume_router` and `bulk_upload_router` but **never actually called `app.include_router(...)` for either.** Confirmed directly against the live OpenAPI schema — zero `/airs/resumes` or `/airs/bulk-uploads` paths existed anywhere. This meant **every HTTP endpoint from Phase B2 through B7, and all of Epic 1's individual resume upload, had been completely unreachable via the real API this entire time** — masked because Redis's unavailability had already forced every prior phase's verification to go through direct service/task-layer calls rather than real HTTP requests, so the gap was never exercised until this phase's own OpenAPI-schema inspection surfaced it. Fixed by restoring both `include_router` calls.
- **Verification:** all 3 new routes confirmed present in the live OpenAPI schema post-fix; `list_history` (pagination correctness across two pages), `get_job_detail` (job + per-file breakdown), and `export_history` (real non-trivial `.xlsx` bytes, correct audit log) all confirmed directly against real data; 404s confirmed for both a missing job and a missing campaign.

---

## Phase B9 — Error Handling for New Failure Modes

**Before:** Trace every exception path in `BULK_EXTRACT` end-to-end and close whatever the individual per-file task (`BULK_RESUME_PARSE`) already handled correctly but the extraction task didn't.

**After:**
- **Modified:** `app/tasks/bulk_upload_tasks.py` (`extract_bulk_upload_zip`'s outer exception handler, new `_cleanup_orphaned_uploads` helper)
- **Bug found and fixed:** any unhandled exception during extraction *other than* the two already-explicit cases (corrupt ZIP, too many files) — e.g. a Supabase Storage outage on download, or a failure partway through staging files — left the job stuck at `EXTRACTING` forever, invisible as "failed" to the brand-new B8 history/detail view, and orphaned any files already uploaded to storage before the failure (no DB row ever existed to reference or clean them up). Fixed: the job now always moves to `FAILED` with a real `error_summary` for any unhandled exception, and every file uploaded earlier in that same failed run is deleted from storage first.
- **Deliberately out of scope (and why):** extending Epic 1's circuit-breaker pattern to bulk storage/encryption failures was considered and dropped — its own audit-log-on-open path references `EntityType.CIRCUIT_BREAKER`, which doesn't exist in the Python enum *and* was never added to the live Postgres enum either (already flagged as an unfinished Epic 1 Phase 11 loose end in this very document, above). Completing that is a different epic's follow-up, not this one's. A periodic sweep to detect Celery-worker-crash "stuck" jobs was also considered and dropped — a distinct monitoring feature, not per-request error handling, and this codebase has no equivalent sweep for any other async pipeline either.
- **Verification (live):** a job pointed at a nonexistent storage object correctly moved to `FAILED` (previously would have stayed at `EXTRACTING` forever); a real 3-file ZIP with a forced failure on the 3rd upload correctly deleted the first 2 already-uploaded files from storage (confirmed via `file_exists` returning `False` for both afterward), created zero orphaned `bulk_upload_job_files` rows, and moved the job to `FAILED` with the real error message.

---

## Cross-Cutting Issues Discovered Across Epic 2

### 1. `AuditRepository` has no `.commit()` method
Found in B4. A stray call in the new bulk-parse task silently turned successful parses into recorded task failures (though the actual DB writes still landed via a later commit on the same shared session). Fixed by removing the call — matches the established pattern where any repository sharing the session can commit the rest.

### 2. `RetryDriver` hardcoded its Dead Letter Queue `task_type`
Found in B6, in pre-existing JD-pipeline infrastructure being reused for the first time outside JD. Every bulk-upload DLQ entry would have been mislabeled `JD_DOCUMENT_PROCESSING`. Fixed by adding a constructor parameter; JD's own call site was updated to pass its literal string explicitly so its behavior is unchanged.

### 3. `EntityType` missing four members that the live database already supported
Found in B6. `CANDIDATE`, `RESUME`, `CONSENT`, `BULK_UPLOAD_JOB` were all present in the live `audit_entity_type_enum` but absent from the Python enum — apparently dropped by unrelated, parallel skill-ontology work editing the same file. This silently broke B4's own audit logging (and plausibly Epic 1's individual-resume audit logging too) the moment those entity types were actually referenced. Restored — a pure Python-side fix, no DB changes needed.

### 4. `bulk_upload_job_files` had no state equivalent to `CeleryTaskLog.RUNNING`
Found in B7, via this phase's own testing before it shipped. Without a `RUNNING` state, a file whose task was genuinely in-flight could be bulk-cancelled underneath it and then have its outcome silently overwritten once the task finished. Closed with an atomic conditional claim (`try_start_processing`) plus a new `RUNNING` enum value — the same protection `CeleryTaskLog.RUNNING` already gives campaign-pause's in-flight work.

### 5. Alembic multi-head divergence (pre-existing, not fixed)
Found across B0 and again in B7. Parallel, unrelated JD/skill-ontology work branched the migration graph from the same point as this epic and never merged it back. One of those unmerged chains has its own pre-existing schema-drift bug (a column that already exists on `job_descriptions`). This epic's own migrations were applied directly against the DB and `alembic_version` was `stamp`'d to the correct revision each time, deliberately without attempting to fix or merge the other, unrelated chains — that's other work's cleanup, not this epic's.

### 6. `main.py` never actually registered `resume_router` or `bulk_upload_router`
Found in B8 — the single most consequential issue in this epic. Both routers were imported but `app.include_router(...)` was never called for either, meaning **every bulk-upload endpoint (B2–B7) and all of Epic 1's individual resume upload were unreachable via the real HTTP API** the entire time. This is directly attributable to Cross-Cutting Issue #7 below: with no way to make a real end-to-end HTTP request in this environment, nothing ever exercised the actual route registration until B8's OpenAPI-schema inspection caught it by chance. Fixed by restoring both `include_router` calls.

### 7. Redis unavailable throughout this development environment
Every phase's Celery task (`extract_bulk_upload_zip`, `parse_bulk_upload_file`) was verified by invoking it directly rather than through a real `apply_async` + worker round-trip, since `redis-server` was never running here. This is a genuine environment gap, not a code defect, but it's the reason Cross-Cutting Issue #6 survived undetected for six phases — worth a real end-to-end smoke test (Redis + a worker + a live HTTP request) before this epic is considered production-verified.

### 8. Real transient Gemini `503 UNAVAILABLE`, again
Observed twice in B4, on the same `AI_EXTRACTION` step Epic 1 had already hit this exact issue on. Confirms it's a genuine, recurring external condition rather than a fluke — B6's retry/backoff machinery exists specifically to absorb this.

---

## Summary Table — Epic 2, All 10 Phases

| Phase | Focus | New HTTP surface | Bugs found outside own scope |
|---|---|---|---|
| B0 | Schema & config foundations | No | Broken Alembic chain (pre-existing) |
| B1 | ZIP validation & job repository | No | — |
| B2 | Bulk upload intake API | `POST /bulk-uploads` | — |
| B3 | `BULK_EXTRACT` Celery task | No (background task) | — |
| B4 | `BULK_RESUME_PARSE` (parse-first per-file task) | No (background task) | — |
| B5 | `MAX_FILES_PER_ZIP` cap enforcement | No (extends B3's task) | — |
| B6 | Partial-failure handling + Dead Letter Queue | No (extends B4's task) | `RetryDriver` hardcoded `task_type`; `EntityType` missing 4 members |
| B7 | Cancellation | `POST /bulk-uploads/{id}/cancel` | Alembic multi-head divergence (pre-existing) |
| B8 | Bulk upload history | `GET /bulk-uploads`, `GET /bulk-uploads/export`, `GET /bulk-uploads/{id}` | `main.py` never registered `resume_router`/`bulk_upload_router` |
| B9 | Error handling for new failure modes | No (hardens B3's task) | — |

---

## Known Gaps / Follow-Up Work — Epic 2

- **Full HTTP + Redis end-to-end smoke test** — every phase was verified at the service/task layer or via direct `task.run(...)` calls; a real `apply_async` → worker → HTTP round-trip (with Redis actually running) has not been performed in this environment. Given Cross-Cutting Issue #6, this is the single highest-value remaining verification step.
- **Epic 1's own already-flagged gaps** (email/alerting module, OCR for image resumes, `CIRCUIT_BREAKER` DB enum values, M16 compliance stories, skill extraction/embeddings, connection-pool sizing) — unchanged, still open, listed in full above.
- **Alembic chain reconciliation** — three divergent heads still exist (this epic's, JD's, and skill-ontology's); one of the other two has its own pre-existing schema-drift bug. Neither was fixed here, as both belong to different work.
- **Circuit-breaker coverage for bulk storage/encryption failures** — deliberately not extended to bulk uploads in B9, since the underlying mechanism (`EntityType.CIRCUIT_BREAKER`) is itself an incomplete Epic 1 loose end.
- **Stale/stuck-job detection** — no periodic sweep exists to detect a job left permanently at `EXTRACTING`/`PROCESSING`/`RUNNING` by a hard Celery-worker crash (as opposed to a caught exception, which B9 now handles correctly). Epic 1 hit this exact scenario once and recovered manually; the same manual-recovery approach would still be needed for bulk uploads today.
- **Epic 3** — explicitly deferred by the user until Epic 2 was complete; not started, no data provided yet.
