# Resume Intake Epic (M05) — Implementation Plan

**Module:** M05 – Resume Intake | Individual Resume Upload
**Related module:** M16 – Compliance & Consent (consent-capture scope only)
**Prepared:** 2026-07-14
**Status:** Planning — not yet implemented

## Constraints

- The database already exists. **No migrations are created or required.**
- Implementation uses **existing tables only**: `candidates`, `resumes`, `resume_parse_attempts`, `candidate_consent`, `encryption_keys`, `campaign_candidates`, `campaign_candidate_stage_history`, `celery_task_log`, `document_processing_stage_executions`, `platform_config`, `audit_log`, `circuit_breaker_state`.
- Dependent modules that aren't fully built yet (e.g. jurisdiction consent configuration, an admin key-rotation UI) are satisfied with **seeded rows read through the repository layer**, not new admin surfaces.
- The plan is broken into small, **independently testable phases** — each phase can be verified in isolation (unit test, direct service call, or a single API call) without depending on a later phase being complete.

---

## Phase 0 — Config & Enum Foundations

**Objective:** Get every constant value and audit vocabulary the rest of the epic needs into the existing tables/enums before any service code is written.

**Files to modify:**
- `app/enums/constants.py` — add `RESUME_UPLOADED`, `CONSENT_RECORDED`, `UPLOAD_BLOCKED_ERASURE_REQUEST` to `ActionType`; add `CANDIDATE`, `RESUME`, `CONSENT` to `EntityType`
- `app/seeds/seed_platform_config.py` — add rows: `RESUME_MAX_SIZE_MB`, `CONSENT_VERSION`, `JURISDICTION_CONSENT_CONFIG` (JSON-encoded string, since `platform_config.value` is `String` not `JSONB` — parsed/serialized at the application layer, no schema change)

**Files to create:**
- `app/seeds/seed_encryption_key.py` — inserts one `ACTIVE` row into the existing `encryption_keys` table (`purpose=CANDIDATE_PII`)

**Components reused:** existing `PlatformConfig` model/table, existing seed-script pattern (`seed_users.py` / `seed_platform_config.py`)

**Expected outcome:** Querying `platform_config` returns all keys the epic needs; `ActionType`/`EntityType` importable with new members; one active `EncryptionKey` row exists.

**Risks:** Storing `JURISDICTION_CONSENT_CONFIG` as a JSON string in a plain `String` column has no type safety — a malformed manual edit breaks parsing silently until a consumer hits it. Acceptable tradeoff given "no migrations."

---

## Phase 1 — Encryption Service Foundation

**Objective:** Stand up PII encryption and dedup-hashing as a standalone, independently testable capability, with no dependency on Candidate/Resume code yet.

**Files to modify:** none

**Files to create:**
- `app/repositories/encryption_key_repository.py` — `get_active_by_purpose`, `get_rotating_by_purpose` over the existing `encryption_keys` table
- `app/core/encryption.py` — resolves raw key bytes from `.env`/`candidate_pii_key` by `key_alias`
- `app/core/encryption_service.py` — `EncryptionService.encrypt(value, purpose)`, `.generate_hash(value)` (MD5, normalized), ACTIVE→ROTATING fallback logic per `KeyStatus`

**Components reused:** `EncryptionKey` model/table, `cryptography` library (already in `requirements.txt`, unused until now), `HashService`'s SHA-256 pattern as structural template

**Expected outcome:** Unit-testable in isolation — encrypt/decrypt round-trips correctly, hash is deterministic and normalized, fallback to `ROTATING` key works when no `ACTIVE` row exists, clean error raised when neither exists. No HTTP surface yet.

**Risks:** Key material sourced from a single `.env` value is a single point of failure — losing it makes all encrypted PII permanently unrecoverable. Real KMS integration is a follow-up hardening item, not a blocker for MVP.

---

## Phase 2 — Consent Repository & Service

**Objective:** Make consent capture a real, callable capability, independently testable against a seeded candidate row before `CandidateService` exists.

**Files to modify:** none

**Files to create:**
- `app/repositories/consent_repository.py` — `create` (insert-only), `get_latest_by_candidate`
- `app/services/compliance/consent_service.py` — `record_consent(candidate_id, source, jurisdiction, ip_address, user_agent)`, `is_adequate(candidate_id, jurisdiction)`

**Components reused:** `CandidateConsent` model/table, `PlatformConfig`/`ConfigRepository` for version lookups

**Expected outcome:** Testable by seeding one dummy `candidates` row directly and confirming `record_consent` inserts correctly and `is_adequate` correctly flags stale versions.

**Risks:** `CandidateConsent.consent_source` is a free `String(100)`, not a DB enum — a typo'd source value silently breaks later aggregation. Mitigate with an application-level constant set even though the column can't be constrained without a migration.

---

## Phase 3 — Candidate Repository & Service

**Objective:** Deliver the atomic "create a candidate with encrypted PII + consent, or safely reuse an existing one" capability — the hard-blocker convergence point.

**Files to modify:** none

**Files to create:**
- `app/repositories/candidate_repository.py` — `get_by_email_hash`, `create`, `update_erasure_fields`
- `app/services/resume/candidate_service.py` — `CandidateService.get_or_create(...)`: checks `email_hash` for existing/erasure-blocked candidates, encrypts via `EncryptionService`, hashes via `EncryptionService.generate_hash`, inserts `Candidate` + calls `ConsentService.record_consent` in one transaction, rolls back both on any failure

**Components reused:** `EncryptionService` (Phase 1), `ConsentService` (Phase 2), `Candidate` model/table, existing `try/except: repo.rollback(); raise` transaction pattern

**Expected outcome:** Callable directly with test candidate data — `full_name_encrypted`/`email_encrypted` populated, plaintext never appears in logs or return values, `email_hash` correctly dedupes, erasure-blocked candidates rejected, a consent row always exists whenever a candidate row does.

**Risks:** `email_hash` has a `UNIQUE` constraint — concurrent uploads of the same candidate race on insert. Needs the same `IntegrityError`/`SAVEPOINT`-scoped catch-and-retry pattern `SkillRepository.upsert_unknown_skill` already uses.

---

## Phase 4 — File Validation Service

**Objective:** Build format/size/integrity checks as a pure, DB-independent capability, fully unit-testable with sample files before touching storage.

**Files to modify:**
- `requirements.txt` — add a magic-byte MIME library (e.g. `python-magic`); none exists today

**Files to create:**
- `app/services/resume/file_validation_service.py` — `FileValidationService.validate(file_bytes, filename)`: magic-byte format detection vs. claimed extension, size check against `RESUME_MAX_SIZE_MB`, integrity/corruption/password-protection check per format (`pypdfium2`/`python-docx`/`Pillow` open-attempt)

**Components reused:** `PlatformConfig`/`ConfigRepository` for the size limit, the open-attempt pattern already used in `TextExtractionService`

**Expected outcome:** Unit-testable with a fixture set (valid PDF/DOCX/PNG/JPEG, mislabeled file, oversized file, password-protected PDF, truncated/corrupt file) — each returns the specific rejection reason required.

**Risks:** PNG/JPEG resumes can be validated but **cannot be parsed into text later** without an OCR engine — no OCR library exists in `requirements.txt`. Flagged now; addressed as a scoping decision in Phase 8.

---

## Phase 5 — Resume Repository & Upload Service (Sync Leg Only)

**Objective:** Wire file storage + candidate creation + resume record creation into one orchestrated flow — deliberately stopping short of Celery so this phase is testable on its own.

**Files to modify:** none

**Files to create:**
- `app/repositories/resume_repository.py` — `create`, `get_by_id`, `get_active_by_candidate`, `record_parse_attempt`
- `app/services/resume/resume_service.py` — `ResumeService.upload(...)`: `FileValidationService.validate` → `StorageService.upload_file` → `CandidateService.get_or_create` → `ResumeRepository.create` (`parse_status=PENDING`)

**Components reused:** `FileValidationService` (Phase 4), `CandidateService` (Phase 3), existing `StorageService`/`get_storage_service` (Supabase-backed, unchanged), `Resume` model/table

**Expected outcome:** Callable directly with a sample file + candidate payload — file lands in the storage bucket, `resumes` row created with correct `file_path`/`file_hash`/`file_format`, `parse_status` sits at `PENDING`.

**Risks:** If storage upload succeeds but the subsequent DB insert fails, the file is orphaned in the bucket with no DB row referencing it. No cleanup job exists — accepted risk, matching current JD-upload behavior.

---

## Phase 6 — Campaign-Candidate Pipeline Hardening

**Objective:** Fix two known correctness gaps in the existing pipeline-entry code (fake idempotency key, missing stage history, race-prone cap check) so the resume flow can safely reuse it — testable against the *existing* manual "add candidate" feature without touching Resume Intake at all.

**Files to modify:**
- `app/repositories/campaign_candidate_repository.py` — add a stage-history insert method; add an idempotency-aware create (return existing row on key collision)
- `app/repositories/CampaignRepository.py` — add a `SELECT ... FOR UPDATE` locking read for the cap check
- `app/services/campaign/campaign_candidate_service.py` — replace the placeholder `idempotency_key=str(uuid.uuid4())` with a deterministic hash of `campaign_id+candidate_id+resume_id`; insert the stage-history row in the same transaction; use the locking repository call for the cap check

**Files to create:** none

**Components reused:** the entire existing `CampaignCandidateService`/`CampaignCandidateRepository`

**Expected outcome:** Existing `POST /campaign-candidates` behavior unchanged from the caller's perspective, but now retry-safe and race-safe; a new `campaign_candidate_stage_history` row appears for every insert. Existing tests for this route must still pass.

**Risks:** This is the one phase touching a shared, already-in-production service. Regression risk on the existing "manually add candidate to campaign" feature — needs existing test coverage re-run before and after the change.

---

## Phase 7 — Upload Orchestration API (Synchronous Leg End-to-End)

**Objective:** Expose the first real HTTP endpoint, chaining Phases 3–6 together, deliberately without Celery yet — a resume can be uploaded and sits at `PENDING`, fully testable via a real API call.

**Files to modify:**
- `app/main.py` — register the new router
- `app/dependencies/resume.py` (new, see below) — wired into the app's DI graph

**Files to create:**
- `app/schemas/resume/request.py` — `ResumeUploadRequest` (candidate fields + consent flag, with field/consent validators)
- `app/schemas/resume/response.py` — `ResumeUploadAcceptedResponse`
- `app/dependencies/resume.py` — `get_candidate_repository` → `get_candidate_service`, `get_resume_repository` → `get_resume_service`, composed into `get_resume_intake_service`
- `app/services/resume/resume_intake_service.py` — orchestrates: campaign ACTIVE/cap/duplicate validation (reusing hardened `CampaignCandidateService`, resequenced to run before storage) → `ResumeService.upload` → `CampaignCandidateService.create_campaign_candidate` → `AuditService.log(RESUME_UPLOADED)`
- `app/api/routes/resume_routes.py` — `POST /resumes`

**Components reused:** everything from Phases 0–6; `require_roles(HR_ADMIN, RECRUITER)`; `APIResponse` envelope

**Expected outcome:** A real `POST /airs/resumes` call with a valid file + candidate payload produces a `candidates` row, a `resumes` row (`PENDING`), a `campaign_candidates` row (`UPLOADED`, with stage history), a `candidate_consent` row, and an `audit_log` entry — verifiable via a Postman/pytest call, no Celery involved yet.

**Risks:** Without the Celery enqueue wired in yet, every uploaded resume permanently sits at `PENDING` until Phase 8 lands — fine for testing this phase in isolation, but don't expose this phase to production users standalone.

---

## Phase 8 — Resume Processing Pipeline & `RESUME_PARSE` Celery Task

**Objective:** Add the async leg — text extraction and structured parsing — completing the upload's actual purpose, and wire the enqueue call from Phase 7 into it.

**Files to modify:**
- `app/core/celery_app.py` — register the new task module in `conf.imports`
- `app/services/document_processing/text_extraction_service.py` — generalize dispatch from `JDSourceFormat`-only to also handle `FileFormat` (PDF/DOCX; PNG/JPEG deferred — see risk)
- `app/services/resume/resume_intake_service.py` — add the actual `.delay()` call now that the task exists

**Files to create:**
- `app/services/resume/resume_processing_context.py` — dataclass mirroring `JDProcessingContext`
- `app/services/resume/resume_processing_pipeline.py` — `ResumeProcessingPipeline`: text extraction → Gemini-based structured parse (new prompt/schema) → writes `Resume.parsed_json`/`parse_confidence_score`/`parser_version`/`page_count`/`ocr_used` → logs each attempt to `ResumeParseAttempt`
- `app/tasks/resume_processing_tasks.py` — `process_resume_document` task, structurally copied from `process_jd_document` (dual sessions: business writes vs. `StageExecutionService` stage tracking with `document_type=DocumentType.RESUME`)

**Components reused:** `StageExecutionService`/`DocumentProcessingRepository` (already document-type-agnostic, no changes needed), `CeleryTaskLogService`, `GeminiExtractionService` pattern, `AuditService`

**Expected outcome:** Triggering the task against an uploaded PDF/DOCX resume produces a populated `parsed_json`, `parse_status=PARSED`, a `ResumeParseAttempt` row, and a full set of `document_processing_stage_executions` rows — testable by invoking the task directly against a fixture resume, without needing the HTTP layer.

**Risks:** PNG/JPEG resumes cannot be meaningfully parsed in this phase — there is no OCR library in `requirements.txt` (only `Pillow`, which can open/validate images but not extract text). Recommend scoping this phase's parsing to PDF/DOCX only, with image-format resumes landing in a clearly flagged failed/manual-review state until an OCR dependency is added as separately-scoped follow-up work.

---

## Phase 9 — Processing Status Polling Endpoint

**Objective:** Let the frontend poll upload progress, closing the loop opened in Phase 7/8.

**Files to modify:** none beyond the router file below

**Files to create:**
- Extend `app/schemas/resume/response.py` — `ResumeProcessingStatusResponse` (reuse `StageProgress` from the JD schemas if generic enough — it already is document-type-agnostic in shape)
- `app/services/resume/resume_processing_status_service.py` — combines `CeleryTaskLogRepository` + `DocumentProcessingRepository` reads, mirroring `JDProcessingStatusService`
- Add `GET /resumes/processing-status/{task_id}` to `app/api/routes/resume_routes.py`

**Components reused:** `CeleryTaskLogRepository`, `DocumentProcessingRepository` — both already generic

**Expected outcome:** Polling mid-processing shows per-stage progress; polling after completion shows `PARSED`/`FAILED` with the final `parsed_json` reference — testable by polling during/after a Phase 8 task run.

**Risks:** Low. Prefer a parallel, resume-specific status service over a shared generalized one unless duplication becomes a real maintenance problem — avoids coupling JD status polling to resume status polling.

---

## Phase 10 — Error Handling, Exception Types & Retry Safety

**Objective:** Turn every validation/orchestration failure into the specific, actionable error the epic's error-handling story requires, and confirm retries are now safe.

**Files to modify:**
- `app/exception_handler/handlers.py` — register handlers for the new exception types
- `app/main.py` — add the new `app.add_exception_handler(...)` registrations

**Files to create:**
- `app/exceptions/resume_exceptions.py` — distinct exception subclasses: unsupported format, size exceeded, corrupt/password-protected file, campaign paused/closed, cap reached, duplicate candidate, encryption unavailable, storage unavailable — following the `CampaignException`/`DuplicateJDException` pattern

**Components reused:** the existing two-tier exception architecture (`app/exceptions/` + `app/exception_handler/`)

**Expected outcome:** Hitting the upload endpoint with each bad-input scenario returns the exact, distinguishable message the epic specifies; resubmitting the same payload after a transient failure (via Phase 6's real idempotency key) returns the existing record instead of creating a duplicate.

**Risks:** Low, mechanical phase — main risk is missing one of the eight specified failure scenarios and letting it fall through to a generic 500. Verify all eight explicitly before closing this phase.

---

## Phase 11 — Infra Resilience: Circuit-Breaker Tracking (Stretch)

**Objective:** Start populating the already-existing `circuit_breaker_state` table on repeated storage/encryption failures, without yet building the email-alerting half (no infrastructure exists for that today).

**Files to modify:**
- `app/services/resume/resume_service.py` — on `StorageException`/encryption failure, increment `circuit_breaker_state.failure_count` for the relevant `service_name`, transition to `OPEN` past `failure_threshold`

**Files to create:**
- `app/repositories/circuit_breaker_repository.py` — `get_by_service_name`, `increment_failure`, `reset`, over the existing `circuit_breaker_state` table

**Components reused:** `CircuitBreakerState` model/table (schema-complete, currently unused), `AuditService` (log the `OPEN` transition here instead of emailing, since no email module exists yet)

**Expected outcome:** Repeated simulated storage failures flip the row to `OPEN` and produce an audit log entry — verifiable by querying `circuit_breaker_state` directly after inducing failures in a test environment.

**Risks:** Without email alerting, this phase has no human-visible surface beyond a DB query or the audit log — low priority. Consider deferring past initial launch.

---

## Phase Summary

| Phase | Focus | Depends on | New HTTP surface |
|---|---|---|---|
| 0 | Config & enum foundations | — | No |
| 1 | Encryption service | Phase 0 | No |
| 2 | Consent repository & service | Phase 0 | No |
| 3 | Candidate repository & service | Phases 1–2 | No |
| 4 | File validation service | Phase 0 | No |
| 5 | Resume repository & upload service (sync) | Phases 3–4 | No |
| 6 | Campaign-candidate pipeline hardening | — (parallelizable with 1–5) | No (existing route only) |
| 7 | Upload orchestration API | Phases 3–6 | Yes — `POST /resumes` |
| 8 | Resume processing pipeline & Celery task | Phase 7 | No (extends existing endpoint) |
| 9 | Processing status polling endpoint | Phase 8 | Yes — `GET /resumes/processing-status/{task_id}` |
| 10 | Error handling & retry safety | Phase 7 | No (hardens existing endpoint) |
| 11 | Circuit-breaker tracking (stretch) | Phase 5 | No |

---

## Addendum — Epic 2 (M05-E02: Bulk ZIP Upload) Schema Changes

**Context:** Epic 1 (above) was built entirely under the "no migrations, existing tables only" constraint. Analyzing Epic 2 (Bulk ZIP Upload) surfaced three genuine schema gaps that Epic 1's existing tables cannot support — correlating an individual resume/task back to the bulk job it came from, and durably recording bulk-upload consent. These were reviewed with the user and **approved as an explicit, scoped exception** to Epic 1's "no migrations" default. Epic 2 will use a real Alembic migration for these three additive, nullable columns.

**Approved schema changes:**

| Table | New column | Type | Purpose |
|---|---|---|---|
| `resumes` | `bulk_upload_job_id` | nullable UUID, FK → `bulk_upload_jobs.id` | Correlates a resume back to the bulk job it came from — needed for per-job failure lists (S04-T02), the processed-before-cancellation list (S05-T03), and file-level history breakdown (S06-T01). Without it, none of these can be reconstructed from the database after the fact. |
| `celery_task_log` | `bulk_upload_job_id` | nullable UUID, FK → `bulk_upload_jobs.id` | Correlates files that fail validation *before* a `resumes` row is ever created (during ZIP extraction) back to their bulk job — needed so S04-T02's failure list includes pre-resume validation failures, not just post-resume parse failures. |
| `bulk_upload_jobs` | `consent_confirmed` | boolean, not null, default `false` | Durably records the mandatory bulk-consent checkbox (S01-T01) on the job record itself — the column did not previously exist despite the consent requirement being explicit in the epic text. |

**Why these are additive/low-risk:** all three are nullable (or default-valued) columns on existing tables — no data migration, no backfill required, no impact on any existing row or Epic 1 code path. `resumes.bulk_upload_job_id` and `celery_task_log.bulk_upload_job_id` are `NULL` for every row Epic 1 already created (individual uploads), which is exactly correct — those resumes/tasks never belonged to a bulk job.

**Not yet applied as of this writing** — implementation is scheduled for Epic 2's own Phase B0 (Schema & Config Foundations), which will include the actual Alembic migration file.

**Related architectural decision (no schema impact, recorded here for continuity):** Epic 2's per-file parse pipeline cannot reuse Epic 1's `process_resume_document` Celery task as-is, since that task assumes `resume_id`/`candidate_id` already exist. Bulk-uploaded files have no recruiter-provided identity — it must come from parsing the file itself — so `candidates`/`resumes` rows are created *after* extraction succeeds (a new, bulk-specific Celery task), not before. Approved in preference to creating placeholder/synthetic candidates and merging them later, which would have required new dedup/merge logic that doesn't exist anywhere in the codebase today.
