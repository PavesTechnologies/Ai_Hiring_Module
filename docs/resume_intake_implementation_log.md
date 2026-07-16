# Resume Intake Epic (M05) — Implementation Log

**Module:** M05 – Resume Intake | Individual Resume Upload
**Scope:** Phases 0–11 of the implementation roadmap
**Status:** All 12 phases implemented. Live-tested end-to-end through a real upload → encryption → storage → pipeline-entry → Celery parse → status-poll cycle.
**Companion document:** `docs/resume_intake_implementation_plan.md` (the original pre-implementation plan)

## How to read this document

Each phase section is split into two halves, matching how the work was actually done:

- **Before implementation** — what was planned: objective, why it was needed, which files were expected to change, which existing components would be reused.
- **After implementation** — what actually happened: files touched, classes/methods/APIs added, verification performed, and any bugs or deviations discovered along the way (several phases surfaced real, pre-existing defects in code outside the phase's own scope — these are called out explicitly where they occurred).

A cross-cutting section after the phase-by-phase log covers bugs and environment issues that spanned multiple phases, plus a running list of what's still open.

---

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
- **Bulk ZIP upload** — a separate epic; `bulk_upload_jobs.consent_confirmed` column doesn't exist yet either.
- **Connection-pool sizing** — worth a deliberate look if exhaustion recurs outside of heavy concurrent debugging sessions.
