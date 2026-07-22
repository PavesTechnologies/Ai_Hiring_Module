# AIRS Resume Intake — UI Tracking & Monitoring API Design

**Status:** Architectural design only. No code, DTOs, repositories, routes, services, SQL, or migrations have been written. Nothing in this document has been implemented.
**Prepared:** 2026-07-22
**Scope:** New, read-only monitoring/tracking endpoints for the frontend, additive to the seven existing production APIs (unmodified, listed below for reference).

**Existing production APIs (unmodified, referenced only):**
```
POST /airs/resumes
GET  /airs/resumes/processing-status/{task_id}
POST /airs/bulk-uploads
GET  /airs/bulk-uploads
GET  /airs/bulk-uploads/export
GET  /airs/bulk-uploads/{bulk_upload_job_id}
POST /airs/bulk-uploads/{bulk_upload_job_id}/cancel
```

**Verification method:** every table/column/enum referenced below was re-confirmed against the live models this session (`app/models/async_tasks.py`, `app/models/candidates.py`, `app/models/pipeline.py`) — nothing here is assumed or invented. Two genuine data gaps were found; they're called out prominently in §8 rather than silently designed around.

---

## 1. API Catalogue

| # | Method | URL | Purpose | Consumer Screen | Role | Response Summary |
|---|---|---|---|---|---|---|
| 1 | GET | `/airs/resumes/{resume_id}` | Full detail for one resume: metadata, candidate summary, current state, parser info, skill summary | Resume Detail | HR_ADMIN, RECRUITER | Single object — see §5 |
| 2 | GET | `/airs/resumes/{resume_id}/timeline` | Per-stage execution timeline for one resume's latest processing attempt | Resume Timeline | HR_ADMIN, RECRUITER | `StageTimeline` — see §4 |
| 3 | GET | `/airs/resumes/{resume_id}/parse-attempts` | Full parse-attempt + failure history across every retry | Retry History | HR_ADMIN, RECRUITER | List of attempt records — see §5 |
| 4 | GET | `/airs/resumes` | Searchable, paginated, filterable resume list | Processing History / Resume List | HR_ADMIN, RECRUITER | Paginated list |
| 5 | GET | `/airs/bulk-uploads/{bulk_upload_job_id}/files` | Paginated, filterable, sortable, searchable per-file list for one job | Bulk Upload Details (file grid) | HR_ADMIN, RECRUITER | Paginated list |
| 6 | GET | `/airs/bulk-uploads/{bulk_upload_job_id}/files/{file_id}` | Full detail for one file inside a bulk job — works even before a `Resume` row exists | Bulk File Detail | HR_ADMIN, RECRUITER | Single object — mirrors §5's resume detail shape |
| 7 | GET | `/airs/bulk-uploads/{bulk_upload_job_id}/files/{file_id}/timeline` | Per-stage timeline for one file inside a bulk job | Bulk File Timeline | HR_ADMIN, RECRUITER | Same `StageTimeline` shape as #2 |
| 8 | GET | `/airs/bulk-uploads/{bulk_upload_job_id}/metrics` | Aggregate processing metrics for one job (stage durations, success/retry rates) | Bulk Upload Dashboard | HR_ADMIN, RECRUITER | Aggregate object |
| 9 | GET | `/airs/bulk-uploads/{bulk_upload_job_id}/failures` | Every failed file in a job with its failure reason, in one call | Failure Detail / triage | HR_ADMIN, RECRUITER | List |
| 10 | GET | `/airs/monitoring/queue-status` | Approximate count of queued/running work across both flows, from the database | Ops / Monitoring Dashboard | HR_ADMIN only | Aggregate counts — DB-approximated, see caveat in §7 |
| 11 | GET | `/airs/monitoring/processing-metrics` | Cross-job, cross-resume aggregate metrics over a bounded time window | Ops / Monitoring Dashboard | HR_ADMIN only | Aggregate object |

Eleven endpoints, all `GET`, all additive. Endpoints #10–11 are restricted to `HR_ADMIN` since they expose operational/infrastructure detail (worker behavior, system-wide throughput) that isn't a recruiting concern — see §9.

---

## 2. UI Mapping

| Screen | Endpoints used |
|---|---|
| **Resume List / Processing History** | #4 (list), each row can deep-link into #1 |
| **Resume Detail** | #1 (primary), #2 (embeds or links to timeline), #3 (retry history tab) |
| **Resume Timeline** | #2 |
| **Retry History** | #3 |
| **Bulk Upload Dashboard** | existing `GET /airs/bulk-uploads` (job list) + #8 (metrics) per visible job |
| **Bulk Upload Details** (existing detail page) | existing `GET /airs/bulk-uploads/{id}` (job header, unchanged) + #5 (paginated file grid, replacing/augmenting the unpaginated embedded file list) |
| **Bulk File Detail** | #6, #7 |
| **Failure Detail** | #9 (job-level triage list), #3 or #7 (single-item failure detail) |
| **Candidate Preview** | Embedded in #1/#6 — no separate endpoint (see §5 rationale; a standalone cross-campaign candidate view is Epic 3 scope, not this one) |
| **Ops / Monitoring Dashboard** | #10, #11 |

---

## 3. Endpoint Design

### #1 — `GET /airs/resumes/{resume_id}`
- **Path variable:** `resume_id: UUID`
- **Query params:** none
- **Response DTO:** see §5 (Resume Detail View)
- **Pagination/Sorting/Filtering:** N/A (single resource)

### #2 — `GET /airs/resumes/{resume_id}/timeline`
- **Path variable:** `resume_id: UUID`
- **Query params:** `attempt_number: int | None` (defaults to latest; historical attempts addressable if the UI wants to compare a past retry against the current one)
- **Response DTO:** `StageTimeline` — see §4
- **Pagination:** none (bounded at 9 stages max)
- **Data-availability caveat:** requires resolving `resume_id → task_id` first. See §8 — this resolution is reliable *after* the task has ever succeeded once, but has a real gap for a resume that is still mid-processing on its *first* attempt or that failed before ever succeeding. Flagged, not silently worked around.

### #3 — `GET /airs/resumes/{resume_id}/parse-attempts`
- **Path variable:** `resume_id: UUID`
- **Query params:** none (result set is inherently small — bounded by `AI_EXTRACTION`'s own 5-attempt policy)
- **Response DTO:** list of `{attempt_number, parser_used, parser_version, status, error_code, error_detail, confidence_score, duration_ms, attempted_at}` (`resume_parse_attempts` columns verbatim) **merged with** `stage_failure_logs` rows for the same resume's task_id, so a resume that failed *before* ever reaching `PERSISTENCE` (and therefore has zero `resume_parse_attempts` rows — see §8) still shows its real failure history.

### #4 — `GET /airs/resumes`
- **Query params:**
  - `campaign_id: UUID | None`
  - `parse_status: ParseStatus | None` (`PENDING` / `PARSING` / `PARSED` / `FAILED`)
  - `source: "individual" | "bulk" | None` (derived from `bulk_upload_job_id IS NULL`)
  - `email_hash: str | None` (exact-match candidate lookup only — see §7/§8 on why name search isn't offered)
  - `uploaded_from`, `uploaded_to: date | None`
  - `page: int = 1`, `size: int = 20` (mirrors the existing `list_bulk_upload_history` pagination convention already in this codebase)
  - `sort_by: "created_at" | "parse_status" = "created_at"`, `sort_dir: "asc" | "desc" = "desc"`
- **Response:** `{items: [...], total: int, page: int, size: int}` — same envelope shape as the existing bulk-upload history list.

### #5 — `GET /airs/bulk-uploads/{bulk_upload_job_id}/files`
- **Path variable:** `bulk_upload_job_id: UUID`
- **Query params:**
  - `status: BulkUploadFileStatus | None` (`QUEUED` / `RUNNING` / `PROCESSED` / `FAILED` / `CANCELLED`)
  - `search: str | None` (matches against `original_filename` — this one *is* plaintext, unlike candidate name, so a simple `ILIKE` is fine)
  - `page`, `size`, `sort_by: "created_at" | "status" | "original_filename" = "created_at"`, `sort_dir`
- **Response:** paginated list — same envelope as #4. This is the endpoint that actually satisfies "Bulk File Tracking" (§6); the file array embedded in the existing `GET /airs/bulk-uploads/{id}` response stays as-is (unmodified, per constraint) but isn't paginated and shouldn't be relied on for large jobs.

### #6 — `GET /airs/bulk-uploads/{bulk_upload_job_id}/files/{file_id}`
- **Path variables:** `bulk_upload_job_id`, `file_id: UUID` (both — validates the file actually belongs to that job, not just that the file ID exists)
- **Response DTO:** mirrors §5's Resume Detail shape, with one structural difference: `candidate`/`resume` sub-objects are `null` until identity is resolved (a file that failed at `AI_EXTRACTION` or on "no identifiable candidate" never gets a `Resume` row at all — see the bulk flow's parse-first architecture).

### #7 — `GET /airs/bulk-uploads/{bulk_upload_job_id}/files/{file_id}/timeline`
- **Path variables:** same as #6
- **Response DTO:** identical `StageTimeline` shape as #2. **No data-availability gap here** — `bulk_upload_job_files.task_id` is populated at row-creation time (added this session), not just on success, so this resolution is reliable at every point in the file's lifecycle. Worth noting as an asymmetry: bulk's timeline lookup is actually more robust today than individual upload's (§8).

### #8 — `GET /airs/bulk-uploads/{bulk_upload_job_id}/metrics`
- **Path variable:** `bulk_upload_job_id: UUID`
- **Response:** `{total_files, processed, failed, duplicate, avg_duration_by_stage: {stage: ms}, retry_rate: float, success_rate: float}` — computed via `GROUP BY stage` over `document_processing_stage_executions` for this job's file task_ids.

### #9 — `GET /airs/bulk-uploads/{bulk_upload_job_id}/failures`
- **Path variable:** `bulk_upload_job_id: UUID`
- **Query params:** `page`, `size` (defensive — most jobs have few failures, but don't assume)
- **Response:** list of `{file_id, original_filename, failed_stage, error_message, classification, retry_count, failed_at}`.

### #10 — `GET /airs/monitoring/queue-status`
- **Query params:** none, or optionally `campaign_id` to scope it
- **Response:** `{resumes_queued, resumes_running, bulk_files_queued, bulk_files_running}` — all `COUNT(*)` queries against `celery_task_log`/`bulk_upload_job_files` filtered by status. **This is a database approximation, not a live broker read** — see §7.

### #11 — `GET /airs/monitoring/processing-metrics`
- **Query params:** `window: "1h" | "24h" | "7d" = "24h"` (bounded — see §7 on why an unbounded aggregate isn't offered without a new background job)
- **Response:** `{throughput_per_hour, avg_duration_by_stage, failure_rate_by_stage, top_failure_reasons}`.

---

## 4. Processing Timeline Design

**Important calibration against the idealized flow in the request:** the requested timeline was `Upload → Queued → Storage → Text Extraction → Cleaning → AI Extraction → Validation → Skill Normalization → Embedding Generation → Persistence → Completed/Failed`. Checked against what's actually tracked today:

| Requested box | Actually tracked as its own row? | Where it really comes from |
|---|---|---|
| Upload | No | Implicit — the resume/file row's own `created_at` |
| Queued | No | `celery_task_log.queued_at` (a timestamp, not a stage row) |
| **Storage** | **No** | Folded into `TEXT_EXTRACTION` — the same stage function downloads the file *and* extracts text in one call, for both flows |
| Text Extraction | Yes | `ProcessingStage.TEXT_EXTRACTION` |
| Cleaning | Yes | `ProcessingStage.TEXT_CLEANING` |
| AI Extraction | Yes | `ProcessingStage.AI_EXTRACTION` |
| **Validation** | Yes, but note the name | `ProcessingStage.JSON_VALIDATION` — a `ProcessingStage.VALIDATION` value also exists in the enum but is **never populated by either pipeline today** (file-format validation happens synchronously, untracked, before any stage runs) |
| Skill Normalization | Yes | `ProcessingStage.SKILL_NORMALIZATION` |
| Embedding Generation | Yes | `ProcessingStage.EMBEDDING_GENERATION` |
| Persistence | Yes | `ProcessingStage.PERSISTENCE` |
| Completed/Failed | No | Derived from `celery_task_log.status` / `resumes.parse_status` |

Per constraint #13: **`Storage` as an independently-timed box, and file-format `Validation` as a tracked stage, do not exist in the data today.** Building them would mean adding a `STORAGE`/`VALIDATION` stage call inside `ResumeProcessingPipeline`/`parse_bulk_upload_file` — explicitly forbidden by constraints #4/#5/#6. The recommended UI treatment: render `Text Extraction` as *"Text Extraction (includes file download)"* and omit a separate file-validation node, or render it as a synchronous pre-flight check with no duration rather than a pipeline stage. No schema change needed either way — this is a presentation decision, not a data gap.

### `StageTimeline` response shape (shared by endpoints #2 and #7)

```
{
  "task_id": "string",
  "document_type": "RESUME",
  "overall_status": "QUEUED | RUNNING | RETRY | PAUSED | SUCCESS | FAILURE | DEAD",
  "current_stage": "AI_EXTRACTION | null",
  "attempt_number": 2,
  "retry_count": 1,
  "progress_percent": 42.9,
  "queued_at": "timestamp",
  "started_at": "timestamp | null",
  "completed_at": "timestamp | null",
  "stages": [
    {
      "stage": "TEXT_EXTRACTION",
      "status": "SUCCESS | FAILED | RUNNING | PENDING | SKIPPED",
      "started_at": "timestamp | null",
      "completed_at": "timestamp | null",
      "duration_ms": 812,
      "attempt_number": 1,
      "error_message": "string | null",
      "skipped": false,
      "retryable": true
    }
  ]
}
```

- `progress_percent` = `(count of stages with status SUCCESS or SKIPPED) / 7 × 100` — computed in the service layer, not stored.
- `retryable` is **not** a stored column — it's derived by joining to `stage_failure_logs.classification` for the matching `(task_id, stage, attempt_number)`: `TRANSIENT → true`, `PERMANENT → false`, `UNKNOWN → null` (honestly surfaced as "unknown," not guessed).
- `stages` only ever contains real, already-written `document_processing_stage_executions` rows — a stage that hasn't started yet simply doesn't appear in the array (no synthetic `PENDING` placeholder rows are fabricated; the UI renders the gap between the last real stage and the full 7-stage list itself).

---

## 5. Resume Detail View

**Response composition for `GET /airs/resumes/{resume_id}` (and, with `resume`/`candidate` nullable, `GET /airs/bulk-uploads/{job}/files/{file}`):**

```
{
  "resume": {
    "id", "file_path", "file_format", "version_number", "is_active_version",
    "parse_status", "parser_version", "page_count", "created_at",
    "bulk_upload_job_id"            // null for individual uploads
  },
  "candidate": {
    "id", "full_name",              // decrypted — HR_ADMIN/RECRUITER only, see §9
    "email",                        // decrypted — same restriction
    "jurisdiction", "consent_given"
  },
  "processing": {
    "current_status": "...",        // from celery_task_log, resolved per §8's caveat
    "current_stage": "...",
    "attempt_number": 2,
    "task_id": "..."
  },
  "skill_summary": {
    "total_skills": 12,
    "matched_exact": 8, "matched_alias": 2, "matched_fuzzy": 1, "unmatched": 1
  },
  "embedding_status": {
    "exists": true, "embedding_model_version_id": "...", "generated_at": "..."
  },
  "parser_info": {
    "parser_used": "gemini-resume-extraction",
    "parser_version": "gemini-resume-extraction-v1"
  },
  "failure": {                      // null unless parse_status == FAILED
    "failed_stage": "AI_EXTRACTION",
    "error_message": "...",
    "classification": "TRANSIENT | PERMANENT | UNKNOWN",
    "moved_to_dlq": true
  }
}
```

All eight fields come from tables that already exist: `resumes`, `candidates`, `celery_task_log`, `candidate_skills` (grouped by `match_tier`), `resume_embeddings`, `dead_letter_queue`/`stage_failure_logs`. No new table required — see §8 for the one real caveat (resolving `current_status`/`task_id` mid-flight).

---

## 6. Bulk File Tracking

Endpoint #5 (`GET /airs/bulk-uploads/{job_id}/files`) is the answer here — designed explicitly to support:
- **Filtering:** by `status` (`QUEUED`/`RUNNING`/`PROCESSED`/`FAILED`/`CANCELLED`)
- **Searching:** by `original_filename` (plaintext column, safe to `ILIKE`)
- **Pagination:** `page`/`size`, same envelope convention as the existing bulk-upload history list
- **Sorting:** by `created_at`, `status`, or `original_filename`

This is a genuinely new capability, not a rename of the existing embedded file array in `GET /airs/bulk-uploads/{id}` — that array stays exactly as it is (unmodified per constraint #1) and remains fine for small jobs; #5 is what a UI should actually page through for a large ZIP.

---

## 7. Performance Review

| Endpoint | Query pattern | Cache? | Paginate? | Reasoning |
|---|---|---|---|---|
| #1 Resume Detail | Live, 3–4 targeted single-row queries (not one giant join) | No | N/A | Real-time accuracy matters for an actively-processing resume; the query is cheap (all lookups are by PK or a unique/indexed FK) |
| #2 Timeline | Live, single query on `document_processing_stage_executions` filtered by `task_id` | No | No (max 9 rows) | The table's own `UNIQUE(task_id, stage, attempt_number)` constraint already serves as the needed index (leftmost column `task_id`) |
| #3 Parse Attempts | Live, two small queries (`resume_parse_attempts` + `stage_failure_logs`) | No | Not needed today (bounded by retry policy) but accept `page`/`size` defensively | Small, bounded result sets |
| #4 Resume List | Live, indexed filter + pagination | No | **Yes, mandatory** | Could be thousands of rows across all campaigns |
| #5 Bulk Files | Live, indexed filter + pagination | No | **Yes, mandatory** | Defensive even though most jobs are small |
| #6 Bulk File Detail | Live | No | N/A | Same reasoning as #1 |
| #7 Bulk File Timeline | Live | No | No | Same reasoning as #2 |
| #8 Job Metrics | Aggregate (`GROUP BY stage`) over one job's task_ids | **Short TTL (30–60s) while job is `PROCESSING`** | N/A | An aggregate doesn't need millisecond freshness, and a dashboard left open will otherwise re-run the same `GROUP BY` every poll |
| #9 Job Failures | Live, small result set | No | Defensive pagination | Failures are the exception, not the common case |
| #10 Queue Status | Live `COUNT(*)` | Short TTL (5–10s) if this becomes a frequently-open ops dashboard | N/A | Cheap individually, but many concurrent dashboard viewers polling every few seconds adds up |
| #11 Processing Metrics | Aggregate over a **bounded time window** (`1h`/`24h`/`7d`) | TTL 60s+ | N/A | An unbounded, always-fresh global aggregate would normally call for a scheduled pre-aggregation job — **explicitly not allowed** (constraint #10). Bounding the window keeps the on-demand query cheap enough to run live instead. |

General principle applied throughout: single-resource detail endpoints stay live (freshness matters, cost is low); list/aggregate endpoints get pagination and, where the data is inherently a snapshot rather than a live status, a short cache window — never a new background job to keep something "pre-computed."

---

## 8. Database Review

### Tables already sufficient, no changes needed
`resumes`, `candidates`, `resume_parse_attempts`, `celery_task_log`, `document_processing_stage_executions`, `stage_failure_logs`, `dead_letter_queue`, `bulk_upload_jobs`, `bulk_upload_job_files` (including its new `task_id` column), `candidate_skills`, `resume_embeddings`, `campaign_candidates`.

### Genuine data gaps found — flagged per constraint #13, not designed around

**Gap 1 — no reliable `resume_id → task_id` resolution while a task is still in flight.** `celery_task_log.resume_id` is only ever set *after* `process_resume_document` succeeds (`task_log.resume_id = processed_resume_id`, written once, in the success path only — confirmed in `app/tasks/resume_processing_tasks.py`). It is never set in either failure branch. `resumes` itself has no `task_id`/`celery_task_id` column. Consequence: for a resume that is still on its first attempt (`RUNNING`/`RETRY`) or that has never yet succeeded, there is no database path from `resume_id` to the `task_id` needed by endpoint #2's timeline. Two ways to close this, **neither implemented here, both requiring approval**:
- (a) Add a nullable `resumes.last_task_id` column, mirroring what was already added to `bulk_upload_job_files.task_id` this session — a schema change.
- (b) Set `task_log.resume_id` at task *start* (right after fetching the resume) instead of only at success — not a schema change, but it does touch `app/tasks/resume_processing_tasks.py`, which is adjacent to (though not itself) the pipeline/`StageExecutionService` files constraints #4–#6 protect.

Bulk upload does **not** have this gap — `bulk_upload_job_files.task_id` is populated at row creation, before any processing starts, so endpoint #7 is reliable at every point in a file's lifecycle. Worth noting as a real asymmetry between the two flows today.

**Gap 2 — `resume_parse_attempts` only ever records successful attempts.** `ResumeRepository.record_parse_attempt` is called exactly once, from inside `ResumeService.persist_processed_resume`'s success path (confirmed in `app/services/resume/resume_service.py`) — a resume that fails *before* reaching `PERSISTENCE` (e.g., a permanently-classified `AI_EXTRACTION` failure) generates **zero** `resume_parse_attempts` rows. The real attempt/failure history for such a resume lives entirely in `stage_failure_logs` (per-attempt) and `dead_letter_queue` (final outcome) instead. Endpoint #3's design already accounts for this by merging both sources — flagged here so the gap is explicit rather than discovered later as "why is Retry History empty for this failed resume."

### Recommended indexes (not created here — recommendations only)
- `bulk_upload_job_files (bulk_upload_job_id, status)` — for endpoint #5's status filter.
- `bulk_upload_job_files (bulk_upload_job_id, original_filename)` — for endpoint #5's filename search, if this table grows large enough for a sequential `ILIKE` scan to matter.
- Verify an index exists on `campaign_candidates (campaign_id, resume_id)` for endpoint #4's `campaign_id` filter, which must join through `campaign_candidates` — `resumes` itself carries no `campaign_id` column.
- `document_processing_stage_executions` needs no new index — its existing unique constraint on `(task_id, stage, attempt_number)` already serves every timeline lookup in this design (`task_id` is the leftmost column).

### Explicitly not recommended
Searching resumes/candidates by decrypted name is **not** offered as a filter in endpoint #4. `candidates.full_name_encrypted` is encrypted at rest — a text search would require either decrypting every row per request (does not scale) or a separate searchable index of plaintext names (a real schema/security decision, out of scope for this design exercise). `email_hash` exact-match is offered instead, since it's already how identity dedup works today.

---

## 9. Security Review

| Endpoint | Sensitive fields | Never expose | Role |
|---|---|---|---|
| #1, #6 (detail) | `candidate.full_name`, `candidate.email` (decrypted PII) | `full_name_encrypted`/`email_encrypted`/`phone_encrypted` raw ciphertext, `encryption_key_id`, `email_hash`/`phone_hash` (correlation-only, not user-facing) | HR_ADMIN, RECRUITER (existing endpoints already draw this line the same way) |
| #2, #7 (timeline) | `error_message` on a failed stage may echo internal detail (file paths, library exception text) | Full stack traces — timeline should carry `stage_failure_logs.message` (a summary), never `dead_letter_queue.full_error_trace` verbatim | HR_ADMIN, RECRUITER |
| #3 (parse attempts) | Same `error_detail` consideration as above | — | HR_ADMIN, RECRUITER |
| #4 (resume list) | Aggregated PII exposure risk if paginated without limits | No unbounded `size` — cap at a fixed maximum (e.g. 100) server-side regardless of what's requested | HR_ADMIN, RECRUITER |
| #5, #9 (bulk files/failures) | `original_filename` (uploader-controlled, could contain a real name — same sensitivity class as a resume filename already exposed by the *existing*, unmodified `GET /airs/bulk-uploads/{id}` endpoint, so no new exposure) | — | HR_ADMIN, RECRUITER |
| #8 (job metrics) | None — pure aggregates | — | HR_ADMIN, RECRUITER |
| #10, #11 (monitoring) | `worker_hostname`, `input_payload_hash`, raw `dead_letter_queue.full_error_trace`/`input_payload` are operational/infrastructure detail | Must never reach a recruiter-facing screen — no `RECRUITER` access to these two | **HR_ADMIN only** |

**Authorization pattern:** every endpoint uses the same `require_roles(...)` dependency the seven existing endpoints already use — no new auth mechanism needed. The one deliberate change from the existing pattern is narrowing #10/#11 to `HR_ADMIN` only, since `RECRUITER` has no legitimate reason to see worker hostnames or system-wide throughput — this role split doesn't exist elsewhere in Resume Intake today and is worth confirming with product before implementation, not assumed silently.

**Cross-tenant consideration:** every list endpoint (#4, #5, #9) must scope by `campaign_id` (or the job's own `campaign_id` for #5/#9) using the same authorization boundary the existing bulk-upload history list already enforces — no new boundary invented, just applied consistently.

---

## 10. Production Readiness Review

**API consistency:** all eleven endpoints follow the same conventions already established by the seven production endpoints — `APIResponse.ok(data=..., message=...)` envelope, `page`/`size` pagination matching `list_bulk_upload_history`'s exact shape, `require_roles` for auth. Nothing here introduces a second convention.

**Naming:** `/airs/resumes/{id}/timeline`, `/airs/resumes/{id}/parse-attempts` and the bulk mirrors follow the same nested-resource pattern the existing `/airs/bulk-uploads/{id}/cancel` already uses — a sub-action or sub-view of a resource is a path segment under it, not a query parameter or a separate top-level route.

**REST compliance:** all eleven are `GET`, all are read-only (no state mutation, per constraint #11), all use path variables for resource identity and query parameters for filtering/paging — no verbs in URLs, no RPC-style endpoints.

**Future extensibility:** the shared `StageTimeline` shape (used identically by #2 and #7) means a *third* document type sharing this same tracking table — the codebase's own comments note `document_processing_stage_executions` was built "document-type-agnostic... so a future Resume pipeline reuses it as-is," and it already has — would get its own timeline endpoint for free, same DTO, same service method, different resolver for `task_id`.

**Backward compatibility:** zero risk — every endpoint here is additive. None of the seven existing endpoints, none of their request/response shapes, and none of the underlying processing/retry/persistence code paths are touched.

---

## Summary of what requires a decision before implementation

Everything in this document is additive and read-only except two items flagged in §8, which need an explicit choice before endpoint #2 can be built as designed:

1. **Gap 1** (individual-upload mid-flight timeline lookup) — pick (a) a new nullable column on `resumes`, or (b) an earlier write of `celery_task_log.resume_id` in `resume_processing_tasks.py`, or (c) accept the limitation (timeline only fully reliable after a resume's first successful completion, same as it effectively is today via the existing status-polling endpoint).
2. **Role split for #10/#11** — confirm `HR_ADMIN`-only is the right boundary, since it's a narrower restriction than any existing Resume Intake endpoint uses today.

Everything else in this catalogue can be implemented against the schema exactly as it stands.
