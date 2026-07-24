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

---

# Epic 3 (M05-E03): Duplicate Detection & Validation — Implementation Plan

**Status:** Planning — not yet implemented. Stories/tasks sourced verbatim from the M05-E03 backlog (S01–S06). M15-E01 fraud-epic stories in the same backlog export are explicitly **out of scope** here; only the M05-E03-tagged S06 rows are covered, and only to the narrow degree they require (flag + route to `FRAUD_REVIEW` — not M15's full weighted `fraud_risk_score` system).

**Pre-implementation audit findings (drives every phase below):**
- Candidate email-hash dedup (S02-T01) is **already fully built** — `candidates.email_hash` has a real DB unique constraint, `CandidateRepository.get_by_email_hash()` + `CandidateService.get_or_create()` already resolve identity. No work needed.
- `resumes.version_number` / `is_active_version` and `campaign_candidates.fraud_flags` / `is_fraud_flagged` columns **exist as unused schema shells** — provisioned in the Epic 1/2 initial migration but never written to by any code path.
- `pipeline_stage` enum already includes `FRAUD_REVIEW` (DB + model), and `campaign_candidate_stage_history` exists — but has exactly one write call site (the initial `UPLOADED` row). No generic transition-with-validation method exists anywhere.
- `allowed_transitions` table exists with a correct schema but **zero seed data and zero read/write code** — the entire stage-transition-validation mechanism is a ground-up build.
- `resumes.file_hash` (MD5) is stored on every resume but **never compared against** — no duplicate-file check exists in either the individual or bulk upload path today.
- ⚠️ **Trap:** `app/enums/constants.py` defines a second, stale `PipelineStage(str, Enum)` that does **not** match the real, DB-backed `PipelineStage` in `app/models/pipeline.py` (missing `FRAUD_REVIEW`, `UPLOADED`, `HOLD`, `HM_REVIEW`, `SELECTED`). All new code must import from `app.models.pipeline`, never from `app.enums.constants`.
- Any new `ActionType`/`EntityType` audit value needs the established two-step treatment: add the Python enum member **and** a companion `ALTER TYPE ... ADD VALUE IF NOT EXISTS` migration (non-transactional) — see `a7c4e9f1d2b8_audit_enum_resume_pipeline_values.py` for the precedent. Adding the Python member alone is not sufficient and will fail at insert time.
- S02-T03's "notify HR_ADMIN via email" requirement depends on an email/alerting module that **does not exist** — already flagged as an open gap in Epic 1 (see Known Gaps in `resume_intake_implementation_log.md`). This blocks the notification *delivery* half of Phase C4; the detection/query half is not blocked.

## Phase C0 — Stage-Transition & Audit Foundations

**Objective:** Build the generic, validated pipeline-stage-transition mechanism every later phase needs (C5's resubmission re-trigger, C7's fraud routing), and get the new audit vocabulary into place before any service code depends on it.

**Files to modify:**
- `app/enums/constants.py` — add `ActionType` members: `DUPLICATE_FILE_DETECTED`, `DUPLICATE_CANDIDATE_LINKED`, `CAMPAIGN_RESUBMISSION_DETECTED`, `RESUME_VERSION_CREATED`, `PIPELINE_STAGE_TRANSITIONED`, `FRAUD_FLAG_RAISED`, `CROSS_CAMPAIGN_ALERT_SENT`
- `app/seeds/seed_platform_config.py` — add `FRAUD_COSINE_THRESHOLD` (0.97), `KEYWORD_DENSITY_THRESHOLD` (0.35), `MAX_SKILLS_COUNT` (60), `MAX_SKILL_REPETITION` (5), `CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD`, `CROSS_CAMPAIGN_SUBMISSION_WINDOW_DAYS`

**Files to create:**
- `alembic/versions/xxxx_audit_enum_duplicate_detection_values.py` — `ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS ...` for each new `ActionType` above, following the existing non-transactional-migration precedent
- `app/seeds/seed_allowed_transitions.py` — seeds the real `(from_stage, to_stage, allowed_roles, requires_reason)` state graph, including `UPLOADED → FRAUD_REVIEW`, `SCREENING → FRAUD_REVIEW`, `FRAUD_REVIEW → REJECTED`, `FRAUD_REVIEW → SCREENING` (cleared-flag return path), alongside the pre-existing non-fraud transitions
- `app/repositories/allowed_transition_repository.py` — `get(from_stage, to_stage) -> AllowedTransition | None`
- `app/services/campaign/pipeline_transition_service.py` — `PipelineTransitionService.transition_stage(campaign_candidate, to_stage, changed_by, reason, source=SYSTEM)`: validates via `AllowedTransitionRepository`, updates `pipeline_stage`, inserts `CampaignCandidateStageHistory` (reusing the existing `create_stage_history` repo method), all in one transaction

**Components reused:** `CampaignCandidateStageHistory` model + existing `create_stage_history()` repo method, `AllowedTransition` model (schema-complete, unused until now), `AuditService`

**Expected outcome:** `allowed_transitions` table populated with the pipeline's real state graph; `PipelineTransitionService` independently callable/testable — an invalid transition raises a clear exception, a valid one updates `pipeline_stage` and writes history atomically; new `ActionType` values usable in `AuditService.log(...)` without enum errors.

**Risks:** The transition graph itself is a design decision I'm inferring from scattered mentions in the story text (no explicit state diagram was provided) — worth confirming the seeded graph with you before it becomes load-bearing for every later phase.

---

## Phase C1 — Resume Versioning Core (S05: T01–T03)

**Objective:** Replace the currently-hardcoded `version_number=1, is_active_version=True` in both upload paths with real version-increment/deactivate logic — the foundation C2, C4, and C5 all build on.

**Files to modify:**
- `app/repositories/resume_repository.py` — add `get_max_version_number(candidate_id)`, `deactivate_active_version(candidate_id)` (atomic `UPDATE ... SET is_active_version = false WHERE candidate_id = :id AND is_active_version = true`, mirroring the atomic-increment pattern already used in `bulk_upload_job_repository.py`), `get_all_versions_by_candidate(candidate_id)`
- `app/services/resume/resume_upload_service.py` — replace the hardcoded version fields with real lookup + deactivate-then-insert logic
- `app/tasks/bulk_upload_tasks.py` — same version-bump logic in the bulk per-file parse path, scoped narrowly to avoid touching the unrelated, still-open B9 orphan-cleanup issue in this file

**Files to create:**
- `app/schemas/resume/response.py` extension — `ResumeVersionHistoryResponse` / `ResumeVersionItem`
- New route on `app/api/routes/resume_routes.py` — `GET /resumes/candidates/{candidate_id}/versions`

**Components reused:** `ResumeRepository.get_active_by_candidate` (already exists), the atomic-UPDATE pattern established for bulk job counters

**Expected outcome:** Uploading a second resume for an existing candidate produces `version_number=2`, deactivates the prior row, in one transaction — verifiable via direct service calls; the version-history endpoint lists all versions with the active one marked.

**Risks:** Must use an atomic UPDATE for deactivation, not read-modify-write — two concurrent version-bump requests for the same candidate could otherwise both read "no active version" and both insert as version N+1, the same lost-update class this codebase has hit before.

---

## Phase C2 — Exact Duplicate Detection: Individual Upload (S01-T02)

**Objective:** Detect a byte-identical re-upload before it's silently processed, and surface the required warning + resolution choice to the uploader.

**Files to modify:**
- `app/repositories/resume_repository.py` — add `get_by_file_hash_global(file_hash)` (unscoped by candidate — the exact-duplicate check is system-wide)
- `app/services/resume/resume_upload_service.py` / `resume_intake_service.py` — compute `file_hash` and check for a match before proceeding; short-circuit into a duplicate-warning response unless the caller has already chosen a resolution
- `app/schemas/resume/request.py` — add `resolution: Literal["use_existing", "upload_anyway"] | None`
- `app/schemas/resume/response.py` — add `DuplicateFileWarningResponse` (filename, original upload date, campaign names, current pipeline stage)
- `app/api/routes/resume_routes.py` — extend `POST /resumes` to return the warning or act on an explicit resolution
- `app/exceptions/resume_exceptions.py` — add `DuplicateResumeFileException`

**Components reused:** `ResumeRepository` (extended in C1), `AuditService`, the existing `hiring_campaigns`/`campaign_candidates` join pattern already used in `campaign_service.py` (for resolving campaign names, never IDs, per the story's display requirement)

**Expected outcome:** Re-uploading an identical file returns a structured duplicate warning instead of creating a new resume; `upload_anyway` creates a new version via C1; `use_existing` links the existing candidate to the new campaign without reprocessing.

**Risks:** Low — "byte-for-byte identical" maps directly onto the already-computed `file_hash`, no new hashing logic needed.

---

## Phase C3 — Exact Duplicate Detection: Bulk Upload (S01-T03)

**Objective:** Auto-skip exact-duplicate files within a ZIP with zero manual intervention, per the story's explicit requirement.

**Files to modify:**
- `app/tasks/bulk_upload_tasks.py` — in the per-file processing path, check `file_hash` against existing resumes before creating a new one; on match, skip resume/candidate creation, call the already-existing (but currently unused) `increment_duplicate_count`, log a `celery_task_log` `SUCCESS` row with the exact specified `output_summary` wording, and link the existing candidate to the campaign if not already linked

**Files to create:** none — extends the existing bulk task only

**Components reused:** `BulkUploadJobRepository.increment_duplicate_count` (already exists, unused until now), `CampaignCandidateService.create_campaign_candidate`, `CampaignCandidateRepository.get_by_campaign_and_candidate`, `ResumeRepository.get_by_file_hash_global` (from C2)

**Expected outcome:** A ZIP containing an already-processed file auto-skips it, increments `duplicate_count`, and links the existing candidate to the campaign if needed — no manual review required.

**Risks:** Touches `bulk_upload_tasks.py`, the same file with the still-open, deliberately-unfixed B9 orphan-cleanup bug — this phase's edits must stay scoped to the duplicate-check branch only.

---

## Phase C4 — Candidate Identity Resolution & Resubmission Alerting (S02-T02, S02-T03)

**Objective:** Drive the version-bump path (C1) whenever an existing candidate is found, and add the daily high-frequency-resubmission detection sweep.

**Files to modify:**
- `app/services/resume/candidate_service.py` — when `get_or_create` resolves an existing candidate, route callers into C1's version-bump path instead of always creating `version=1`

**Files to create:**
- A new scheduled task (e.g. `app/tasks/scheduled/resubmission_alert_task.py`) — daily query grouping `campaign_candidates` by `candidate_id` against `CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD`/`_WINDOW_DAYS`
- `app/core/celery_app.py` — register the beat schedule entry

**Components reused:** `platform_config` keys from C0, `AuditService`

**Expected outcome:** The detection query correctly identifies over-threshold candidates and records an audit event.

**Risks — real blocker:** The story requires emailing HR_ADMIN with the alert. No email/alerting module exists in this codebase (an open gap since Epic 1). This phase can only deliver detection + audit logging; actual email delivery needs a separate, explicitly-scoped piece of infrastructure first. Flagging this now rather than silently under-delivering the story.

---

## Phase C5 — Same-Campaign Resubmission Handling (S03: T01–T03)

**Objective:** Detect an existing campaign+candidate pairing before erroring on the unique constraint, present resolution options, and correctly re-trigger the pipeline on a resume update.

**Files to modify:**
- `app/services/campaign/campaign_candidate_service.py` — check `get_by_campaign_and_candidate` before insert; on a match, return the candidate's current `pipeline_stage` instead of raising; implement the "update resume" path: new version via C1, reset all score fields to `NULL`, `PipelineTransitionService.transition_stage(..., to_stage=UPLOADED, reason="Resume updated — re-evaluation triggered")` (C0), enqueue a new parse task

**Files to create:**
- Resolution request/response schemas under `app/schemas/campaign/`
- A resolution endpoint (extends existing campaign-candidate routes)

**Components reused:** `PipelineTransitionService` (C0), resume versioning (C1), `CampaignCandidateRepository.get_by_campaign_and_candidate` (already exists)

**Expected outcome:** Re-uploading for a candidate already in the campaign surfaces their current stage instead of a raw constraint error; choosing "update resume" creates a new version, resets scores, logs stage history, and re-enqueues parsing; `candidate_skills` from the prior version are retained, never deleted (already true — nothing in the codebase deletes `candidate_skills`).

**Risks:** The story requires an extra HR_ADMIN confirmation gate once a candidate has passed `SHORTLISTED` — needs careful role/stage-gating, not just a straight re-trigger.

---

## Phase C6 — Cross-Campaign Candidate Tracking (S04: T01–T03)

**Objective:** Expose the cross-campaign history view; confirm score isolation is already structurally correct.

**Files to modify:**
- `app/repositories/campaign_candidate_repository.py` — add `get_all_by_candidate_across_campaigns(candidate_id)`, ordered by `created_at desc`

**Files to create:**
- `GET /candidates/{id}/campaign-history` (HR_ADMIN only, via existing `require_roles`) — campaign name, JD title, submission date, stage, `composite_score`, outcome, plus a summary count

**Components reused:** existing `require_roles` dependency, `hiring_campaigns`/`job_descriptions` join pattern

**Expected outcome:** A candidate's full cross-campaign history is visible to HR_ADMIN only; T03 (contamination prevention) needs no new logic — every score/stage field already lives on the per-campaign `campaign_candidates` row — worth a targeted test rather than new code.

**Risks:** Low — mostly additive, read-only work.

---

## Phase C7 — Fraud-Pattern Duplicate Flags (S06: T01–T03, M05-E03 scope only)

**Objective:** Flag near-duplicate resumes (cosine similarity) and keyword-stuffed resumes, routing both to `FRAUD_REVIEW` — without building M15's full weighted risk-scoring system.

**Files to modify:**
- `app/services/resume/resume_processing_pipeline.py` — after embedding generation, run a pgvector ANN cosine-similarity query excluding the candidate's own prior versions; on a match `>= FRAUD_COSINE_THRESHOLD`, append `DUPLICATE_RESUME` to `fraud_flags`, set `is_fraud_flagged=True`, call `PipelineTransitionService.transition_stage(..., FRAUD_REVIEW)`; after parsing, compute keyword density / skill count / repetition against the C0-seeded thresholds and append `KEYWORD_STUFFING` under the same rule

**Files to create:**
- A similarity-search method on `ResumeRepository` (or a new `ResumeEmbeddingRepository`) using the pgvector `<=>` operator
- Scorecard fraud-display schema/endpoint extension, plus clear-flag / confirm-rejection actions (HR_ADMIN only) that call back into `PipelineTransitionService`

**Components reused:** `PipelineTransitionService` (C0), `resume_embeddings` table (already populated by the existing pipeline)

**Expected outcome:** A near-duplicate resume (cosine ≥ 0.97) or a keyword-stuffed one is auto-flagged and routed to `FRAUD_REVIEW`; HR_ADMIN sees the flags on the scorecard and can clear or confirm rejection.

**Risks:** Needs to confirm `rejection_layer` already has a `FRAUD` value usable for "confirm rejection" — not yet verified against the live enum; may need its own small migration if missing. Fully depends on C0's seeded transitions including a valid path into and out of `FRAUD_REVIEW`.

---

## Epic 3 Phase Summary

| Phase | Stories covered | Depends on | New HTTP surface |
|---|---|---|---|
| C0 | Foundation for all | — | No |
| C1 | S05 (T01–T03) | C0 (audit values only) | Yes — `GET /resumes/candidates/{id}/versions` |
| C2 | S01-T02 | C1 | Extends `POST /resumes` |
| C3 | S01-T03 | C1, C2 | No (extends existing bulk task) |
| C4 | S02-T02, S02-T03 | C1 | No (detection/audit only — email delivery blocked, see risk) |
| C5 | S03 (T01–T03) | C0, C1 | Yes — resubmission resolution endpoint |
| C6 | S04 (T01–T03) | — | Yes — `GET /candidates/{id}/campaign-history` |
| C7 | S06 (T01–T03, M05-E03 scope) | C0 | Extends scorecard endpoint |

**Known blocker carried into this epic:** C4's email-alerting half cannot be delivered until an email/alerting module exists (open since Epic 1). Everything else is independently buildable in the C0→C7 order above.
