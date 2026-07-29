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

**Status:** Phases C0–C6 implemented and live-tested. **Phase C7 deliberately deferred — not implemented** (see its section below and the Epic 3 Phase Summary for why). Stories/tasks sourced verbatim from the M05-E03 backlog (S01–S06). M15-E01 fraud-epic stories in the same backlog export are explicitly **out of scope** here; only the M05-E03-tagged S06 rows are covered, and only to the narrow degree they require (flag + route to `FRAUD_REVIEW` — not M15's full weighted `fraud_risk_score` system).

**As-built detail for every implemented phase (files touched, methods added, bugs found, live-test results) lives in the companion `resume_intake_implementation_log.md`, under "Epic 3 (M05-E03): Duplicate Detection & Validation — Phases C0–C6."** This plan document is left as originally written for C0–C6 (a few small deviations discovered during implementation are noted inline below); only C7's section and the status lines have been updated to reflect where the epic actually landed.

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

**As-built note:** only 1 of the 7 speculative `ActionType` members below (`PIPELINE_STAGE_TRANSITIONED`) was actually needed and added — see the Implementation Log for the full as-built detail, including a discovered pre-existing, narrower `StageTransitionService` that this phase's `PipelineTransitionService` now duplicates (flagged, not consolidated).

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

> ## ⛔ NOT IMPLEMENTED — deliberately deferred
>
> **What C7 would do:** automatically flag two kinds of suspicious resumes and route them to `FRAUD_REVIEW` for HR_ADMIN review, without building M15's full weighted `fraud_risk_score` system:
> 1. **Near-duplicate resumes** — a pgvector cosine-similarity search against *other candidates'* resume embeddings; a match at or above a similarity threshold appends `DUPLICATE_RESUME` to `fraud_flags` (e.g. the same resume content resubmitted under a different identity).
> 2. **Keyword-stuffed resumes** — abnormal skill count / keyword repetition in the parsed resume text, appending `KEYWORD_STUFFING` to `fraud_flags`.
>
> Either flag would set `is_fraud_flagged=True` and call `PipelineTransitionService.transition_stage(..., FRAUD_REVIEW)` (C0) — the transition edges for this already exist, seeded back in C0. HR_ADMIN would then see the flags on the candidate scorecard and either clear the flag (false positive, back to `SCREENING`) or confirm it (reject, `rejection_layer=FRAUD`).
>
> **Why deferred:** unlike C0–C6, this phase has no deterministic right answer to build against — it needs two numeric thresholds (a cosine-similarity cutoff, and a keyword-stuffing skill-count/repetition cutoff) that this plan document assumed were already seeded ("C0-seeded thresholds") but in fact were never added anywhere in the codebase. Shipping it now means shipping pure guesses (e.g. the `0.97` cosine figure below has no real resume-similarity data behind it) with no way to validate whether they under- or over-flag real candidates. The recommendation, made and accepted, is to revisit C7 once there's real usage volume to calibrate these thresholds against, rather than ship unvalidated fraud-detection logic that could wrongly block real candidates or miss real fraud.
>
> **A real implementation gap also surfaced during analysis, worth carrying into whenever this phase is picked back up:** the plan below says to hook detection "after embedding generation" *inside* `resume_processing_pipeline.py`. Tracing the actual bulk-upload call path shows this placement doesn't work there — `parse_bulk_upload_file` runs the entire pipeline (including embedding generation) *before* the `campaign_candidate` row is created, so a check embedded mid-pipeline would find no campaign_candidate row to flag/transition during bulk upload, every time. Individual upload, resubmission (C5), and the paused-campaign re-enqueue path don't have this problem — the campaign_candidate row always exists first in those flows. The fix identified (not yet built): implement fraud detection as its own standalone service, called once after the pipeline finishes, from both call sites (`process_resume_document` and `parse_bulk_upload_file`), rather than embedded inside the pipeline class itself.

**Objective:** Flag near-duplicate resumes (cosine similarity) and keyword-stuffed resumes, routing both to `FRAUD_REVIEW` — without building M15's full weighted risk-scoring system.

**Files to modify:**
- `app/services/resume/resume_processing_pipeline.py` — after embedding generation, run a pgvector ANN cosine-similarity query excluding the candidate's own prior versions; on a match `>= FRAUD_COSINE_THRESHOLD`, append `DUPLICATE_RESUME` to `fraud_flags`, set `is_fraud_flagged=True`, call `PipelineTransitionService.transition_stage(..., FRAUD_REVIEW)`; after parsing, compute keyword density / skill count / repetition against the C0-seeded thresholds and append `KEYWORD_STUFFING` under the same rule

**Files to create:**
- A similarity-search method on `ResumeRepository` (or a new `ResumeEmbeddingRepository`) using the pgvector `<=>` operator
- Scorecard fraud-display schema/endpoint extension, plus clear-flag / confirm-rejection actions (HR_ADMIN only) that call back into `PipelineTransitionService`

**Components reused:** `PipelineTransitionService` (C0), `resume_embeddings` table (already populated by the existing pipeline)

**Expected outcome:** A near-duplicate resume (cosine ≥ 0.97) or a keyword-stuffed one is auto-flagged and routed to `FRAUD_REVIEW`; HR_ADMIN sees the flags on the scorecard and can clear or confirm rejection.

**Risks:** Needs to confirm `rejection_layer` already has a `FRAUD` value usable for "confirm rejection" — not yet verified against the live enum; may need its own small migration if missing. Fully depends on C0's seeded transitions including a valid path into and out of `FRAUD_REVIEW`.

**As-built note:** the two risks flagged above are both resolved as non-issues — `RejectionLayer.FRAUD` already exists in the live enum, and all 4 `FRAUD_REVIEW` transition edges this phase needs were already seeded by C0 (confirmed during C7's analysis pass). The blocker was never these; it's the missing thresholds and the bulk-upload timing gap described above.

---

## Epic 3 Phase Summary

| Phase | Stories covered | Depends on | New HTTP surface | Status |
|---|---|---|---|---|
| C0 | Foundation for all | — | No | ✅ Implemented |
| C1 | S05 (T01–T03) | C0 (audit values only) | Yes — `GET /resumes/candidate/{id}/versions` | ✅ Implemented |
| C2 | S01-T02 | C1 | Extends `POST /resumes` | ✅ Implemented |
| C3 | S01-T03 | C1, C2 | No (extends existing bulk task) | ✅ Implemented |
| C4 | S02-T02, S02-T03 | C1 | No (detection/audit only — email delivery blocked, see risk) | ✅ Implemented (scope reduced — see log) |
| C5 | S03 (T01–T03) | C0, C1 | Yes — `POST /campaign-candidates/{id}/update-resume` | ✅ Implemented |
| C6 | S04 (T01–T03) | — | Yes — `GET /candidates/{id}/campaign-history` | ✅ Implemented |
| C7 | S06 (T01–T03, M05-E03 scope) | C0 | Extends scorecard endpoint | ⛔ **Not implemented — deferred** |

**Known blocker carried into this epic:** C4's email-alerting half cannot be delivered until an email/alerting module exists (open since Epic 1) — C4 shipped detection + audit logging only, as this document already anticipated.

**Epic 3 close-out status (this update):** C0–C6 are implemented and live-tested against the real database (full detail in `resume_intake_implementation_log.md`). C7 is explicitly deferred, not abandoned — its transition edges, `RejectionLayer.FRAUD` value, and `resume_embeddings` data are all already in place and waiting; only the detection thresholds and the fraud-detection service itself remain unbuilt, deliberately, until real usage data exists to calibrate them against.

---

# Epic 4 (M05-E04): Upload Progress & Tracking — Implementation Plan

**Status:** Planning — not yet implemented. Stories/tasks sourced verbatim from the M05-E04 backlog (S01–S05, T01–T03 each, 15 tasks total).

**Pre-implementation audit findings (drives every phase below):**

- **`CeleryTaskLog` has no `duration_ms` column.** Every phase displaying a task's duration must compute it at read time as `completed_at - started_at`, never assume a stored field.
- **`CeleryTaskLog` has no `input_payload` JSONB column** — only `input_payload_hash` (a hash string). The backlog's S02-T02 assumes the bulk per-file log is "sourced from `celery_task_log.input_payload` JSONB containing `bulk_upload_job_id`" — that path doesn't exist. It doesn't need to: `CeleryTaskLog.bulk_upload_job_id` is already its own real FK column. The live file-log phase queries that column directly, no JSONB digging required.
- **Task-type naming mismatch:** the backlog assumes a task type literally named `RESUME_PARSE`. The real, live value is `RESUME_DOCUMENT_PROCESSING` (`app/tasks/resume_processing_tasks.py`). All new code must use the real value.
- **Email infrastructure now exists — this changes the picture from Epic 1–3.** Since Epic 3 closed, real infrastructure was built for M07-E03's rejection emails: `EmailTemplate`/`EmailNotification` models, an `EmailTriggerEvent` enum (currently only `CANDIDATE_REJECTED`), AWS SES-backed sending (`SESEmailClient`, `boto3`), and a full idempotent/retrying/DLQ-integrated Celery task (`send_candidate_email_task`). Epic 4's notification stories build on this directly — new `EmailTriggerEvent` values (each needing its own DB-enum migration, same pattern as `ActionType`), new templates, and a de-dup check before queuing (none exists yet) — not a from-scratch email system.
- **No WebSocket infrastructure exists anywhere in this app** (confirmed — zero `@app.websocket`/`WebSocketRoute` usage). The backlog offers polling as an explicit alternative ("poll every 15 seconds via the API **or** update via WebSocket push") — every phase below uses polling against a REST endpoint, satisfying the requirement without new infrastructure.
- **Circuit-breaker service-name mismatch, decided:** the backlog's platform dashboard names `MINIO`/`EMBEDDING_SERVICE`/`GEMINI_FLASH` as tracked services. Only `SUPABASE_STORAGE` and `ENCRYPTION_SERVICE` are actually instrumented today — embedding and Gemini extraction have zero circuit-breaker tracking. **Decision (approved):** the dashboard shows only the 2 real, currently-tracked services; `EMBEDDING_SERVICE`/`GEMINI_FLASH` are flagged as a real, separate instrumentation gap, out of scope for this epic.
- **S01-T03 has no trigger condition to fire on, decided:** `AIEvaluationStatus.COMPLETED` is never set anywhere in this codebase — no `AI_EVALUATE` Celery task exists at all (only deterministic scoring is built; AI evaluation is a separate, not-yet-built module). **Decision (approved):** this task is explicitly deferred, mirroring Epic 3's C7 treatment — no dangling, untestable infrastructure built for a trigger that can't fire yet.
- **No combined individual+bulk upload history query exists anywhere.** `BulkUploadJobRepository` only ever queries `bulk_upload_jobs` rows; there is no join/union with individually-uploaded `Resume` rows. S03's "unified history" is a ground-up build, not an extension of an existing query.
- **`ExcelExport` already supports clean multi-sheet exports** (`_write_sheet` helper, already used for a 3-sheet report) — both new exports in this epic (S03-T03's 2-sheet, S05-T03's 4-sheet) reuse this helper directly; no new Excel-writing framework needed.
- **`DeadLetterQueue`'s schema matches the backlog's assumed field names exactly** (`input_payload`, `final_error_message`, `replayed_at`, `replayed_by` all real columns) — low risk there. What's missing is read-side aggregation (`get_by_id`, a "list unresolved" query) and an individual-upload equivalent of the bulk-only replay method that already exists (`BulkUploadService.replay_failed_file`).
- **No `UserRepository` class exists anywhere**, and no "list all active HR_ADMIN users" query exists — needed fresh for both S04-T03's notification recipients and S05-T01's dashboard audience.
- **No user notification-preference field exists.** S01-T03 mentions "based on the RECRUITER's notification preference setting" — moot since S01-T03 itself is deferred (see above); flagged here in case a future phase revisits it.
- **A closely-named-but-different config key already exists:** `DEAD_TASK_ALERT_THRESHOLD` (campaign-scoped health alert, value `5`) is NOT the same thing as S05-T02's `DAILY_DEAD_TASK_ALERT_THRESHOLD` (platform-wide, daily-windowed). New phases must seed the latter as its own key, never reuse or confuse the two.
- **`GET /campaigns/{id}/processing-status`, `/dead-letter-queue`, `/timeline`, and `OpsMonitoringService`'s two platform-wide endpoints already exist** and are structurally close to several of this epic's requirements (per-campaign queue/DLQ views, platform queue-status/processing-metrics) — several phases below extend these rather than building parallel new ones.

---

## Phase D0 — Config, Audit & Email Vocabulary Foundations

**Objective:** Get every new config key, audit action type, and email trigger event into place before any service code depends on them — mirroring C0's role in Epic 3.

**Files to modify:**
- `app/enums/constants.py` — add `ActionType` members: `UPLOAD_HISTORY_EXPORTED`, `RESUME_UPLOAD_RETRIED`, `INDIVIDUAL_UPLOAD_DLQ_REPLAYED`, `PLATFORM_ALERT_SENT`
- `app/models/email.py` — add `EmailTriggerEvent` member: `UPLOAD_PERMANENTLY_FAILED` (not `AI_EVALUATION_COMPLETED` — deferred with S01-T03)
- `app/seeds/seed_platform_config.py` — add `QUEUE_BACKLOG_ALERT_THRESHOLD`, `PARSE_DURATION_ALERT_THRESHOLD_MS`, `DAILY_DEAD_TASK_ALERT_THRESHOLD`, `ALERT_COOLDOWN_HOURS`, `MAX_AI_RETRY_COUNT` (values to be confirmed with the user before this phase is approved — none are specified in the backlog text)
- `app/seeds/seed_email_templates.py` (or wherever templates are seeded) — add the `UPLOAD_PERMANENTLY_FAILED` template

**Files to create:**
- `alembic/versions/xxxx_audit_enum_upload_tracking_values.py` — `ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS ...` for each new `ActionType` above, non-transactional, following the established precedent
- `alembic/versions/xxxx_email_trigger_enum_upload_failed.py` — same pattern for `email_trigger_event_enum ADD VALUE 'UPLOAD_PERMANENTLY_FAILED'`

**Components reused:** the exact non-transactional-migration pattern already used for every prior `ActionType`/`EmailTriggerEvent` addition.

**Expected outcome:** every later phase's audit/email/config dependency already exists and is importable/writable before that phase's own service code is written.

**Risks:** the five new `platform_config` threshold values have no backlog-specified defaults — needs explicit confirmation before seeding, the same way C0's transition graph needed sign-off in Epic 3.

---

## Phase D1 — Individual Upload Live Status on the Candidate List (S01-T01)

**Objective:** Surface `parse_status` and `pipeline_stage` as a live-pollable status on the campaign candidate list, satisfying the "update in real time... without manually refreshing" requirement via polling (no WebSocket infrastructure exists or is being built).

**Files to modify:**
- `app/schemas/campaign/campaign_candidate_schema.py` — add `parse_status: str | None` to `CampaignCandidateResponse` (currently absent — the list response has `pipeline_stage` but nothing from the linked `Resume` row)
- `app/services/campaign/campaign_candidate_service.py` — `get_campaign_candidates` joins in `Resume.parse_status` alongside the existing fields
- `app/repositories/campaign_candidate_repository.py` — extend the existing candidate-list query with the `Resume` join (if not already joined)

**Files to create:** none — extends the existing `GET /campaign-candidates/campaign/{campaign_id}` list endpoint

**Components reused:** the existing candidate-list endpoint and response schema; `parse_status` already exists on `Resume`, `pipeline_stage` already exists and is already returned.

**Expected outcome:** polling the existing list endpoint every 15s (a frontend contract, not new backend work) shows `parse_status` transitioning `PENDING → PARSING → PARSED`/`FAILED` and `pipeline_stage` transitioning `UPLOADED → SCREENING` as the pipeline progresses, using data that already updates correctly today — this phase only adds visibility, not new state transitions.

**Risks:** Low, additive-only. Must confirm the join doesn't introduce an N+1 query on a list endpoint already serving potentially large campaigns.

---

## Phase D2 — Processing Timeline on the Candidate Scorecard (S01-T02)

**Objective:** Show every `celery_task_log` row for a candidate's resume/campaign-candidate pairing, in order, with computed duration and a retry affordance.

**Files to modify:**
- `app/repositories/celery_task_log_repository.py` — add `get_by_campaign_candidate_id(campaign_candidate_id)` (broader than the existing task-type-scoped method), ordered by `queued_at asc`
- `app/schemas/campaign/campaign_candidate_schema.py` — add `ProcessingTimelineEntry` (`task_type`, `status`, `queued_at`, `started_at`, `completed_at`, `duration_display: str | None` — computed, human-readable, e.g. "2.3 seconds", never a raw `duration_ms` since no such column exists — `error_message`, `can_retry: bool`) and add `processing_timeline: list[ProcessingTimelineEntry]` to `CandidateScorecardResponse`
- `app/services/campaign/campaign_candidate_service.py` — `get_campaign_candidate_scorecard` populates the new field

**Files to create:** none — extends the existing scorecard endpoint

**Components reused:** `GET /campaign-candidates/{id}` (existing scorecard endpoint), `CeleryTaskLog` (existing table, no schema change)

**Expected outcome:** the scorecard response includes a chronological list of every processing step for this candidate, with in-progress steps identifiable (`status=RUNNING`, no `completed_at` yet) and failed steps carrying `error_message` + `can_retry=True`.

**Risks:** Low. "Animated Running indicator with elapsed time" is a frontend concern — backend only needs to expose `started_at` and `status=RUNNING` correctly, which it already can.

---

## Phase D3 — AI-Evaluation-Completed Notification (S01-T03)

> ## ⛔ NOT IMPLEMENTED — deferred
>
> **What this would do:** send an in-platform + email notification to the uploading RECRUITER exactly once, when a `campaign_candidates` row reaches `ai_evaluation_status=COMPLETED` with a `composite_score` populated — including the score, `ai_recommendation`, and a scorecard link.
>
> **Why deferred:** `AIEvaluationStatus.COMPLETED` is never set anywhere in this codebase today. No `AI_EVALUATE` Celery task exists at all — only deterministic scoring is built; AI evaluation (the actual scoring stage this story's trigger depends on) is a separate, not-yet-built module. Building this notification now would mean building a de-dup check, a template, and a service method that sit permanently unreachable until that other module exists and calls them — the same reasoning Epic 3's C7 was deferred under.
>
> **What's already in place, waiting for whenever this is picked back up:** the email infrastructure itself (`EmailNotification`/`EmailTemplate` models, `SESEmailClient`, the retrying/DLQ-integrated `send_candidate_email_task`) needs no new framework — only a new `EmailTriggerEvent.AI_EVALUATION_COMPLETED` value, a template, and a call to `queue_...` from wherever the AI-evaluation task eventually sets `ai_evaluation_status=COMPLETED`.

---

## Phase D4 — Bulk Upload Live Progress Bar (S02-T01)

**Objective:** Expose progress-bar math, per-outcome counts, a status badge, and an ETA for an in-flight bulk upload job.

**Files to modify:**
- `app/schemas/bulk_upload/response.py` — add `BulkUploadProgressResponse` (`percent_complete`, `processed_count`, `failed_count`, `duplicate_count`, `remaining_count`, `status`, `estimated_completion_at: datetime | None`)
- `app/services/bulk_upload/bulk_upload_service.py` — add `get_job_progress(job_id)`: computes `percent_complete` from existing counters, `remaining_count = total_files - processed - failed - duplicate`, and an ETA from `(now - job.created_at) / resolved_count * remaining_count` when `status=PROCESSING` and `resolved_count > 0`
- `app/api/routes/bulk_upload_routes.py` — add `GET /bulk-uploads/{id}/progress`

**Files to create:** none

**Components reused:** every counter field already exists and is already atomically maintained (Epic 2); this is a pure read/computation layer, no new writes.

**Expected outcome:** polling this endpoint every 10s (frontend contract) drives a live progress bar and ETA using data that's already correctly maintained today.

**Risks:** Low. ETA is a simple linear estimate — explicitly not a sophisticated model; worth stating that plainly in the response or docs so it isn't mistaken for a precise prediction.

---

## Phase D5 — Bulk Upload Live File Processing Log (S02-T02)

**Objective:** Stream individual per-file outcomes for an in-flight or completed bulk job, most recent first, paginated.

**Files to modify:**
- `app/repositories/celery_task_log_repository.py` — add `get_by_bulk_upload_job_id(job_id, limit, offset)`, ordered by `completed_at desc nulls first` (or `queued_at desc` for in-progress entries)
- `app/schemas/bulk_upload/response.py` — add `BulkUploadFileLogEntry` (`filename`, `result` — derived from `status`/`output_summary`, `reason: str | None`, `timestamp`)
- `app/services/bulk_upload/bulk_upload_service.py` — add `get_file_log(job_id, limit=50, offset=0)`
- `app/api/routes/bulk_upload_routes.py` — add `GET /bulk-uploads/{id}/file-log`

**Files to create:** none

**Components reused:** `CeleryTaskLog.bulk_upload_job_id` (already a real FK column — no JSONB digging needed, correcting the backlog's assumed schema), `bulk_upload_job_files` for `filename` resolution (already exists, per Epic 2)

**Expected outcome:** the log endpoint returns the 50 most recent file outcomes by default, with `limit`/`offset` for "Load More," persisting correctly after job completion since it's a plain read of already-durable rows.

**Risks:** Low. "New entries appear automatically without a page refresh" is a frontend polling concern; backend just needs a stable, paginated, freshly-queryable endpoint, which this delivers.

---

## Phase D6 — Confirm Server-Side Resilience to Browser Disconnect (S02-T03)

**Objective:** Confirm, not build — bulk processing is already entirely server-side (Celery-driven, `bulk_upload_jobs` as the persistent state tracker), so a client disconnect already cannot interrupt it.

**Files to modify:** none expected — this phase is a targeted verification pass, not new code, mirroring C6's T03 treatment in Epic 3 ("needs no new logic, worth a targeted test rather than new code").

**Files to create:** none expected, unless verification surfaces a real gap (e.g. the one client-side case the backlog itself calls out: a disconnect *during* the initial ZIP upload, before the job row even exists — that failure mode is inherently client-side and may just need frontend-side handling, not a backend change).

**Components reused:** existing bulk-upload architecture (Epic 2).

**Expected outcome:** a documented confirmation (via a real test — start a job, kill the process/connection driving polling, confirm the job continues and completes, confirm `GET /bulk-uploads/{id}` reflects the final state on return) that this requirement is already met, with no code changes needed.

**Risks:** Low. Only real risk is discovering an actual gap during verification — if so, this phase's scope would need revisiting before implementation, not silently expanded.

---

## Phase D7 — Unified Upload History View (S03-T01, S03-T02)

**Objective:** Build the one genuinely new query this epic needs — a combined, filterable, chronological individual+bulk upload history for a campaign. No existing query does this today.

**Files to modify:**
- `app/repositories/resume_repository.py` — add a method resolving individual (non-bulk) uploads for a campaign: `Resume` rows joined to `CampaignCandidate` (for `pipeline_stage`) where `Resume.bulk_upload_job_id IS NULL`, scoped to the campaign via `CampaignCandidate.campaign_id`
- `app/repositories/bulk_upload_job_repository.py` — reuse `get_all_by_campaign` (already exists)

**Files to create:**
- `app/schemas/upload_history/response.py` (new) — `UnifiedUploadHistoryEntry` (a tagged union or `upload_type: Literal["individual","bulk"]` discriminator field, plus the per-type display fields the backlog specifies), `UnifiedUploadHistoryResponse` (paginated, plus applied-filter echo)
- `app/services/upload_history/upload_history_service.py` (new) — `UploadHistoryService.get_history(campaign_id, uploaded_by=None, date_from=None, date_to=None, upload_type=None, outcome=None)`: fetches both sources, merges, filters, sorts by `created_at desc`
- New route (extends `campaign_routes.py` or a new file) — `GET /campaigns/{campaign_id}/upload-history`

**Components reused:** existing `Resume`/`CampaignCandidate`/`BulkUploadJob` data — no schema changes; `require_roles` for the RECRUITER "can only cancel their own pending uploads" constraint (cancel itself is out of this phase's scope — it's D9/D10's territory — this phase is read/filter only).

**Expected outcome:** one endpoint returns a correctly merged, filterable, chronologically-ordered view of every upload (individual and bulk) for a campaign; filter combinations narrow the result set correctly; filter state persistence across a session is a frontend concern, not backend.

**Risks:** Medium — this is the epic's largest net-new query, merging two structurally different row types into one paginated response. Needs care around consistent pagination when merging two different underlying queries (recommend: fetch both fully filtered, sort/merge in Python, then paginate the merged list — acceptable at expected campaign upload volumes; revisit if a campaign's upload count ever grows large enough to make full materialization expensive).

---

## Phase D8 — Upload History Export (S03-T03)

**Objective:** A 2-sheet XLSX export of the unified history, respecting active filters, with zero PII.

**Files to modify:**
- `app/utils/excel_export.py` — add `export_upload_history(individual_rows, bulk_rows)` using the existing `_write_sheet` helper (already supports multi-sheet)
- `app/services/upload_history/upload_history_service.py` — add `export_history(...)`, reusing the same filter logic as D7, audit-logging via the new `ActionType.UPLOAD_HISTORY_EXPORTED` (D0)

**Files to create:** none beyond the route addition — `GET /campaigns/{campaign_id}/upload-history/export`

**Components reused:** `ExcelExport._write_sheet` (already multi-sheet-capable), the existing export-then-audit-log pattern from `BulkUploadService.export_history`

**Expected outcome:** HR_ADMIN gets a downloadable XLSX with the two sheets exactly as specified (candidate UUIDs only, no PII), respecting whatever filters were active; the export is audit-logged with the filter context in `details`.

**Risks:** Low, mechanical — same shape as Epic 2's B8 export, just two sheets and a merged data source instead of one.

---

## Phase D9 — Failed Uploads Aggregation View (S04-T01)

**Objective:** One view aggregating every kind of upload failure for a campaign — individual parse failures, DLQ entries, and bulk jobs with any failures — with dismiss/retry affordances.

**Files to modify:**
- `app/repositories/dead_letter_queue_repository.py` — add `get_by_id`, `list_unresolved_by_campaign(campaign_id)` (joins through `resume_id`/`campaign_candidate_id` to scope by campaign; neither exists today, only `get_by_task_id`)
- `app/repositories/resume_repository.py` — add a campaign-scoped `list_failed(campaign_id)` (parse_status=FAILED)

**Files to create:**
- `app/schemas/upload_history/response.py` extension — `FailedUploadEntry` (`file_identifier`, `failure_type`, `error_reason`, `failed_at`, `retry_count`, `available_actions`, `dismissed: bool`)
- A dismiss mechanism — the backlog requires "Dismissed failures must be hidden... accessible via a Show Dismissed toggle," but nothing in the schema today has a per-failure dismissed flag; needs either a new nullable `dismissed_at` column (on `resumes`/`dead_letter_queue`, additive, low-risk migration) or an application-level dismiss-tracking table — **explicit decision needed before this phase is approved**, not assumed silently.
- New route — `GET /campaigns/{campaign_id}/failed-uploads`

**Components reused:** `GET /campaigns/{campaign_id}/dead-letter-queue` (already exists, campaign-scoped DLQ list) as a partial precedent; `BulkUploadJobRepository.list_by_campaign` (existing, for the `failed_count > 0` bulk rows)

**Expected outcome:** one endpoint surfaces every current failure across all three sources with a total count badge; dismissed items are hidden by default and recoverable via a toggle.

**Risks:** Medium — the "dismiss" mechanism has no existing schema support anywhere in this codebase; needs an explicit design decision (new column vs. new table) before implementation, not an assumption.

---

## Phase D10 — Retry Individual Upload + DLQ Replay (S04-T02)

**Objective:** Give individual uploads the same replay capability bulk files already have (`BulkUploadService.replay_failed_file`), plus a plain re-parse retry for resumes that are simply `parse_status=FAILED` (not yet DLQ'd).

**Files to modify:**
- `app/services/resume/resume_upload_service.py` (or a new `resume_retry_service.py`) — add `retry_parse(resume_id, actor_id)`: validates `parse_status=FAILED`, inserts a new `ResumeParseAttempt` row (`record_parse_attempt` already supports this), sets `parse_status=PENDING`, re-dispatches `process_resume_document` against the existing `file_path` (no new file upload)
- Same service — add `replay_from_dlq(dlq_id, actor_id)`: mirrors `BulkUploadService.replay_failed_file`'s exact pattern — confirm the DLQ entry, re-enqueue under a fresh `task_id` using the stored `input_payload`, call `dead_letter_queue_repo.mark_replayed(...)` (already exists), audit-log `ActionType.RESUME_UPLOAD_RETRIED`/`INDIVIDUAL_UPLOAD_DLQ_REPLAYED` (D0)

**Files to create:** none beyond routes — `POST /resumes/{id}/retry`, `POST /dead-letter-queue/{id}/replay` (or nested under campaign-candidates, matching wherever D9's view lives)

**Components reused:** `DeadLetterQueueRepository.mark_replayed` (already exists, exact semantics needed), `ResumeRepository.record_parse_attempt` (already exists), `BulkUploadService.replay_failed_file` as the direct structural template

**Expected outcome:** a failed individual upload can be retried without re-uploading the file; a DLQ-exhausted one can be replayed from its stored payload; both show up correctly in D2's Processing Timeline as new attempts.

**Risks:** Low — mirrors an already-proven, already-shipped pattern (bulk-file replay) applied to a new but structurally identical case.

---

## Phase D11 — Persistent Failure Notification (S04-T03)

**Objective:** Email the uploader and every active HR_ADMIN when a task reaches `DEAD` (retries exhausted), distinguishing transient vs. permanent failure.

**Files to modify:**
- `app/services/celery_task_log_service.py` — `mark_dead` triggers the new notification path (wrapped in try/except so a notification failure never affects the already-committed DEAD state transition, matching the established pattern from `_queue_rejection_email`)
- Add (wherever it best fits — likely a small new repository since none exists) a "list all active HR_ADMIN users" query
- `app/models/email.py` / template seed — new template for `UPLOAD_PERMANENTLY_FAILED` (D0)

**Files to create:**
- A notification service method (e.g. on a new `upload_failure_notification_service.py`) that: resolves the uploader + all active HR_ADMIN as recipients, classifies transient vs. permanent (reusing Epic 2's existing `error_classifier.classify()` — already built for exactly this distinction), and queues one `EmailNotification` per recipient via the existing `send_candidate_email_task` infrastructure

**Components reused:** `send_candidate_email_task` (already fully built — idempotent, retrying, DLQ-integrated), `error_classifier.classify()` (already exists from Epic 2's retry machinery)

**Expected outcome:** every DEAD task correctly fans out an email to the uploader and all HR_ADMIN, with the classification and a working link to D9's Failed Uploads view; recorded in `email_notifications` with `trigger_event=UPLOAD_PERMANENTLY_FAILED`.

**Risks:** Medium — "all active HR_ADMIN users" as a fan-out recipient list is new territory (no existing multi-recipient send pattern in this codebase; `send_candidate_email_task` was built for one candidate → one recipient). Needs confirming whether to loop and queue N individual `EmailNotification` rows (recommended — keeps the existing one-notification-per-row model intact) or extend the schema for a multi-recipient notification, which would be a heavier, unnecessary change.

---

## Phase D12 — Platform-Wide Upload Queue Dashboard (S05-T01)

**Objective:** One HR_ADMIN-only dashboard aggregating queue depth, task counts, and circuit-breaker health across the whole platform.

**Files to modify:**
- `app/services/ops_monitoring_service.py` — extend with: total `PENDING` resumes, total `PROCESSING` bulk jobs (both new counts, current service doesn't compute these), the 2 real circuit breakers only (per the approved decision — `EMBEDDING_SERVICE`/`GEMINI_FLASH` explicitly not shown, flagged as a separate gap)
- `app/repositories/circuit_breaker_repository.py` — add `get_all()` (no "list all" method exists today)
- `app/api/routes/monitoring_routes.py` — extend `GET /monitoring/queue-status` or add a new dashboard-specific endpoint

**Files to create:**
- A per-campaign queue-depth breakdown query (new — reuses the shape of the existing per-campaign `get_processing_status_summary`/`get_task_status_counts`, generalized to run across all campaigns at once rather than one at a time)

**Components reused:** `GET /campaigns/{id}/processing-status` (existing per-campaign precedent, generalized platform-wide), `CircuitBreakerRepository` (existing, just needs a list method added)

**Expected outcome:** HR_ADMIN sees platform-wide queue metrics, a per-campaign breakdown ordered by queue depth descending, and a real, honest circuit-breaker section (2 services, not the 3 the backlog assumed); a banner appears when either real circuit breaker is `OPEN`.

**Risks:** Low-medium. The per-campaign-breakdown-across-all-campaigns query needs a reasonable cap/pagination if the platform ever has many active campaigns simultaneously — worth deciding a sane limit (e.g. top 20 by queue depth) rather than returning unbounded rows.

---

## Phase D13 — Platform Bottleneck Alerting (S05-T02)

**Objective:** A 30-minute Celery beat task checking 3 platform-wide thresholds, emailing HR_ADMIN, with a cooldown so the same condition doesn't spam repeatedly.

**Files to modify:**
- `app/core/celery_app.py` — register the new beat entry
- `app/enums/constants.py` — `ActionType.PLATFORM_ALERT_SENT` (D0)

**Files to create:**
- `app/tasks/campaign_tasks.py` (or a new dedicated file) — `evaluate_platform_upload_bottlenecks()`: checks queued `RESUME_DOCUMENT_PROCESSING` count vs. `QUEUE_BACKLOG_ALERT_THRESHOLD`, average task duration (computed, no stored `duration_ms`) vs. `PARSE_DURATION_ALERT_THRESHOLD_MS`, and 24h `DEAD` count vs. `DAILY_DEAD_TASK_ALERT_THRESHOLD` — each condition independently cooled down via `ALERT_COOLDOWN_HOURS`
- A cooldown-tracking mechanism — **decision needed:** the simplest option with no new schema is querying `audit_log` for the most recent `PLATFORM_ALERT_SENT` entry matching this specific condition within the cooldown window (reuses existing infrastructure, no migration); the alternative is a small dedicated `platform_alert_state` table if querying audit_log for this purpose proves awkward. Recommend the audit_log-based approach first, given how consistently this codebase prefers reusing existing tables over adding new ones for "last time X happened" tracking.

**Components reused:** `send_candidate_email_task`-style fan-out to all HR_ADMIN (same recipient-list need as D11 — should share the same "list all active HR_ADMIN" query once built), `AuditService.log`

**Expected outcome:** a real backlog, a real slowdown, or a real DEAD-count spike each independently trigger one alert email per condition, at most once per `ALERT_COOLDOWN_HOURS`, all audit-logged.

**Risks:** Medium — three independent threshold checks each need their own cooldown state; the audit-log-query approach must correctly scope "this specific condition" (e.g. by a stable `details` key), not just "any platform alert," to avoid one condition's cooldown suppressing another's alert.

---

## Phase D14 — Platform Upload Metrics Export (S05-T03)

**Objective:** A 4-sheet XLSX operational report — daily volumes, processing performance (including percentiles), failure summary, and hourly queue-depth history.

**Files to modify:**
- `app/utils/excel_export.py` — add `export_platform_upload_metrics(...)` (4 calls to the existing `_write_sheet` helper)

**Files to create:**
- A new metrics-aggregation service/repository method per sheet:
  - Daily volumes — straightforward `GROUP BY date` over `Resume`/`BulkUploadJob` creation timestamps.
  - Processing performance percentiles (`p95`) — **needs a decision**: Postgres `percentile_cont` computed in SQL (efficient, one query) vs. pulling raw durations and computing in Python (simpler code, more data transferred). Recommend SQL-side `percentile_cont` given this is an aggregate-over-potentially-many-rows report.
  - Failure summary — grouped by `error_code`/`error_message` across `CeleryTaskLog`/`DeadLetterQueue`.
  - **Hourly queue-depth history is the hardest of the four** — queue depth at a point in time isn't a stored value anywhere; it can only be *approximated* after the fact from `queued_at`/`started_at`/`completed_at` timestamps (e.g. "count of tasks where `queued_at <= hour AND (completed_at IS NULL OR completed_at > hour)`"), which is a real reconstruction, not a simple read. This needs explicit sign-off on the approximation approach before implementation, not a silent assumption.
- New route — `GET /monitoring/upload-metrics/export`

**Components reused:** `ExcelExport._write_sheet`, the existing `OpsMonitoringService.get_processing_metrics` windowed-metrics shape as a structural precedent (extended to percentiles + a full date-range parameter instead of fixed 1h/24h/7d windows)

**Expected outcome:** HR_ADMIN downloads a 4-sheet XLSX for a chosen date range (default last 30 days), audit-logged.

**Risks:** Medium-high, concentrated entirely in the queue-depth-history sheet — this is the one piece of this whole epic that requires reconstructing a value that was never actually stored, from timestamps alone. Worth confirming the approximation method (and its known imprecision) explicitly before building it, rather than presenting it as more authoritative than it can actually be.

---

## Epic 4 Phase Summary

| Phase | Stories covered | Depends on | New HTTP surface | Notes |
|---|---|---|---|---|
| D0 | Foundation for all | — | No | New `ActionType`/`EmailTriggerEvent`/`platform_config` values |
| D1 | S01-T01 | — | No (extends existing list endpoint) | Polling only, no WebSocket |
| D2 | S01-T02 | — | No (extends existing scorecard endpoint) | `duration_ms` computed, not stored |
| D3 | S01-T03 | — | — | ⛔ **Not implemented — deferred** (no AI-evaluation task exists) |
| D4 | S02-T01 | — | Yes — `GET /bulk-uploads/{id}/progress` | Pure computation over existing counters |
| D5 | S02-T02 | — | Yes — `GET /bulk-uploads/{id}/file-log` | Uses `bulk_upload_job_id` FK directly, not JSONB |
| D6 | S02-T03 | — | No | Verification-only phase, mirrors Epic 3's C6-T03 treatment |
| D7 | S03-T01, S03-T02 | — | Yes — `GET /campaigns/{id}/upload-history` | Largest net-new query in this epic |
| D8 | S03-T03 | D7 | Yes — `GET /campaigns/{id}/upload-history/export` | Reuses existing multi-sheet `ExcelExport` pattern |
| D9 | S04-T01 | — | Yes — `GET /campaigns/{id}/failed-uploads` | Dismiss mechanism needs an explicit schema decision |
| D10 | S04-T02 | D9 (view), D0 (audit) | Yes — retry/replay routes | Mirrors existing bulk-file replay pattern exactly |
| D11 | S04-T03 | D0, D10 | No (background trigger) | New "list active HR_ADMIN" query; multi-recipient fan-out is new territory |
| D12 | S05-T01 | — | Yes — extends `/monitoring/queue-status` | Only 2 real circuit-breaker services shown, per approved decision |
| D13 | S05-T02 | D0, D11's HR_ADMIN query | No (background beat task) | Cooldown mechanism reuses `audit_log`, no new table |
| D14 | S05-T03 | D12 | Yes — `GET /monitoring/upload-metrics/export` | Queue-depth-history sheet is an approximation, needs explicit sign-off |

**Carried-forward/known gaps this epic inherits or surfaces:** `EMBEDDING_SERVICE`/`GEMINI_FLASH` circuit-breaker instrumentation remains a real, separate gap (D12); D3 (AI-evaluation-completed notification) remains blocked on a not-yet-built AI-evaluation module, exactly like Epic 3's C7 pattern; D9's dismiss mechanism and D14's queue-depth-history approximation both need one more explicit decision each before their phases can be approved for implementation.
