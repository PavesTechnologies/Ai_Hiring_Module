# AIRS Resume Intake — Complete API Reference (Epics 1–4)

**Audience:** frontend/UI engineers building or maintaining screens against the Resume Intake module.
**Scope:** every API endpoint that exists today as a result of Epic 1 (M05-E01, Individual Resume Upload), Epic 2 (M05-E02, Bulk ZIP Upload), Epic 3 (M05-E03, Duplicate Detection & Validation), and Epic 4 (M05-E04, Upload Progress & Tracking — phases implemented so far: D0–D2, D4, D5–D7, D10, D12). Endpoints belonging to other modules (campaign CRUD, JD, scoring, rejection analytics, skill ontology, prompt templates, etc.) are out of scope and not listed here, **except** where an Epic 4 phase extended an existing endpoint's response with a resume-intake field — those are called out explicitly.
**Companion documents:** `docs/resume_intake_implementation_log.md` (the full phase-by-phase build history behind every endpoint below, including bugs found and fixed), `docs/frontend_integration_spec.md` (application-wide shared conventions — auth, error shapes, pagination envelope — not repeated in full here), `docs/Resume_Intake_Monitoring_API_Design.md` (the original design rationale for the read-only monitoring/tracking endpoints).
**Base path:** every path below is relative to `/airs` (e.g. `POST /resumes` is really `POST /airs/resumes`).

---

## 1. Shared conventions (read once)

**Auth:** every endpoint requires `Authorization: Bearer <JWT>`. No resume-intake endpoint is public.

**Roles:** `HR_ADMIN`, `RECRUITER`, `HIRING_MANAGER` (Hiring Manager has no access to any resume-intake endpoint today — every route below is `HR_ADMIN` and/or `RECRUITER` only).

**Success envelope** (every non-file-download response):
```json
{ "success": true, "message": "Human-readable message.", "data": { /* endpoint-specific */ } }
```

**Error envelope** (application errors — 4xx/5xx raised by this module's own exception classes):
```json
{ "success": false, "message": "Error message.", "data": null }
```
Structured error payloads (e.g. the duplicate-file warning) ride inside this same shape, with the extra detail nested under `data`.

**Pagination envelope** (every paginated list in this module uses one of these two shapes):
```json
{ "items": [ /* or "entries" */ ], "total": 137, "page": 1, "size": 20 }
{ "entries": [ ... ], "total": 137, "limit": 50, "offset": 0 }
```
`page`/`size` is used by list endpoints that predate Epic 4; `limit`/`offset` is used by the newer live-log/history endpoints (D5, D7). Both are pure offset pagination — no cursor.

**REST polling only.** Nothing in Resume Intake uses WebSockets or Server-Sent Events. Every "live" screen (a progress bar, a live file log, a processing timeline, the ops dashboard) is a plain `GET` the UI is expected to call on its own timer. Suggested polling cadences are noted per-endpoint below; they are recommendations, not server-enforced limits.

---

## 2. State machines the UI needs to understand

These four enums drive almost every badge/status pill in the module. Get comfortable with them before wiring up any screen.

### `parse_status` (one `Resume` row)
`PENDING → PARSING → PARSED` (success) or `PENDING → PARSING → FAILED` (failure, may retry back to `PENDING`). Set on the `Resume` row itself; this is what Epic 4 Phase D1 exposed on the candidate list and what most "is this resume done processing?" UI should key off.

### `pipeline_stage` (one `CampaignCandidate` row — the recruiting-workflow stage, independent of parsing)
`UPLOADED → SCREENING → SHORTLISTED → HOLD / HM_REVIEW / INTERVIEW → SELECTED` or `REJECTED`, plus a side state `FRAUD_REVIEW`. **Never conflate this with `parse_status`** — a resume can be fully `PARSED` while its candidate sits at any pipeline stage, and `pipeline_stage` does not change just because parsing finished or failed (confirmed explicitly in D1's live test).

### `BulkUploadStatus` (one `bulk_upload_jobs` row — the whole ZIP job)
`PENDING → EXTRACTING → PROCESSING → COMPLETED` (all files resolved cleanly) `/ PARTIAL_FAILURE` (some succeeded, some failed/duplicated) `/ FAILED` (extraction itself failed, or every file failed) `/ CANCELLED`.

### `BulkUploadFileStatus` (one file inside a ZIP job)
`QUEUED → RUNNING → PROCESSED` (success **or** duplicate — see note below) `/ FAILED / CANCELLED`. There is **no distinct `DUPLICATE` file-status value** — a duplicate file is marked `PROCESSED` with a specific marker in its `output_summary`; the D5 file-log endpoint is what turns this into a UI-facing `"DUPLICATE"` result badge (see §6.4).

### `TaskStatus` (one `celery_task_log` row — the Celery execution itself, one level below either resume flow)
`QUEUED → RUNNING → SUCCESS / FAILURE`, with `RETRY` (transient failure, will re-attempt) and `DEAD` (retries exhausted, sent to the dead letter queue — see D10) as the two states that matter most for retry/replay UI, plus `PAUSED` (a soft-cancel while the parent campaign itself is paused).

---

## 3. Master endpoint index

| # | Method & Path | Epic / Phase | Roles | One-line purpose |
|---|---|---|---|---|
| 1 | `POST /resumes` | Epic 1 (P7), extended Epic 3 (C2) | HR_ADMIN, RECRUITER | Upload one resume into one campaign |
| 2 | `GET /resumes/processing-status/{task_id}` | Epic 1 (P9) | HR_ADMIN, RECRUITER | Poll one processing task's live status |
| 3 | `GET /resumes` | Epic 1 (monitoring) | HR_ADMIN, RECRUITER | Search/filter/paginate every resume |
| 4 | `GET /resumes/{resume_id}` | Epic 1 (monitoring) | HR_ADMIN, RECRUITER | Full detail for one resume |
| 5 | `GET /resumes/{resume_id}/timeline` | Epic 1 (monitoring) | HR_ADMIN, RECRUITER | Per-stage execution timeline |
| 6 | `GET /resumes/{resume_id}/parse-attempts` | Epic 1 (monitoring) | HR_ADMIN, RECRUITER | Full attempt/failure history |
| 7 | `GET /resumes/candidate/{campaign_candidate_id}/parsed-json` | Epic 1 (monitoring) | HR_ADMIN, RECRUITER | Raw AI-extracted resume JSON |
| 8 | `GET /resumes/candidate/{candidate_id}/versions` | Epic 3 (C1) | HR_ADMIN, RECRUITER | Full resume version history |
| 9 | `POST /resumes/{resume_id}/retry` | Epic 4 (D10) | HR_ADMIN | Re-dispatch a FAILED resume |
| 10 | `POST /resumes/dead-letter-queue/{dlq_id}/replay` | Epic 4 (D10) | HR_ADMIN | Replay a dead-lettered resume failure |
| 11 | `POST /bulk-uploads` | Epic 2 (B2) | HR_ADMIN, RECRUITER | Upload a ZIP for bulk processing |
| 12 | `GET /bulk-uploads` | Epic 2 (B8) | HR_ADMIN, RECRUITER | Paginated bulk-upload job history |
| 13 | `GET /bulk-uploads/export` | Epic 2 (B8) | HR_ADMIN, RECRUITER | Excel export of job history |
| 14 | `GET /bulk-uploads/{id}/progress` | Epic 4 (D4) | HR_ADMIN, RECRUITER | Live percent-complete + ETA |
| 15 | `GET /bulk-uploads/{id}/file-log` | Epic 4 (D5) | HR_ADMIN, RECRUITER | Live per-file resolved-outcome log |
| 16 | `GET /bulk-uploads/{id}` | Epic 2 (B8), extended Epic 4 (ad-hoc) | HR_ADMIN, RECRUITER | Full job detail + per-file list |
| 17 | `GET /bulk-uploads/{id}/files` | Epic 1/2 (monitoring) | HR_ADMIN, RECRUITER | Paginated/searchable file grid |
| 18 | `GET /bulk-uploads/{id}/files/{file_id}` | Epic 1/2 (monitoring) | HR_ADMIN, RECRUITER | Full detail for one file |
| 19 | `GET /bulk-uploads/{id}/files/{file_id}/timeline` | Epic 1/2 (monitoring) | HR_ADMIN, RECRUITER | Per-stage timeline for one file |
| 20 | `GET /bulk-uploads/{id}/metrics` | Epic 1/2 (monitoring) | HR_ADMIN, RECRUITER | Aggregate metrics for one job |
| 21 | `GET /bulk-uploads/{id}/failures` | Epic 1/2 (monitoring) | HR_ADMIN, RECRUITER | Paginated failed-file triage list |
| 22 | `POST /bulk-uploads/{id}/files/{file_id}/replay` | Epic 2 (DLQ infra) | HR_ADMIN | Re-enqueue one dead-lettered file |
| 23 | `POST /bulk-uploads/{id}/cancel` | Epic 2 (B7) | HR_ADMIN, RECRUITER | Cancel a not-yet-finished job |
| 24 | `GET /campaigns/{campaign_id}/upload-history` | Epic 4 (D7) | HR_ADMIN, RECRUITER | Unified individual+bulk history |
| 25 | `GET /campaign-candidates/campaign/{campaign_id}` | pre-existing, extended Epic 4 (D1) | *(open)* | Candidate list, incl. live `parse_status` |
| 26 | `GET /campaign-candidates/{id}` | pre-existing, extended Epic 4 (D2) | *(open)* | Candidate scorecard, incl. processing timeline |
| 27 | `POST /campaign-candidates/{id}/update-resume` | Epic 3 (C5) | HR_ADMIN, RECRUITER (stage-gated) | Resubmit a resume for an existing pairing |
| 28 | `GET /candidates/{candidate_id}/campaign-history` | Epic 3 (C6) | HR_ADMIN | Every campaign a candidate has been in |
| 29 | `GET /monitoring/queue-status` | Epic 1 (monitoring) | HR_ADMIN only | Approximate queue depth, both flows |
| 30 | `GET /monitoring/processing-metrics` | Epic 1 (monitoring) | HR_ADMIN only | Cross-job throughput/failure metrics |
| 31 | `GET /monitoring/upload-queue-dashboard` | Epic 4 (D12) | HR_ADMIN only | Platform-wide queue snapshot |
| — | `DELETE /candidates/{candidate_id}` | Compliance, cross-epic | HR_ADMIN | GDPR-style permanent erasure (listed in §8, not epic-numbered) |

31 numbered endpoints plus one cross-cutting compliance endpoint. Every one of them is additive — nothing here removed or broke a pre-existing endpoint's shape (see the implementation log's own repeated "backward compatible" verification note per phase).

---

## 4. Epic 1 (M05-E01) — Individual Resume Upload

### 4.1 `POST /resumes` — Upload a Resume
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Upload modal / Add Candidate flow

**Purpose:** the entry point for adding one candidate's resume to one campaign. Validates the file, stores it, creates or reuses the candidate record (deduped by email), links it into the campaign's pipeline at `UPLOADED`, and enqueues background AI processing. The response comes back immediately — `parse_status` in the response is always `PENDING` at this point; the UI must poll for real progress (endpoint #2, or the candidate list's live `parse_status` from D1).

**Request:** multipart form —
- `campaign_id` (UUID, required), `candidate_full_name`, `candidate_email` (required), `candidate_phone` (optional), `jurisdiction` (default `GLOBAL`), `consent_confirmed` (bool, required), `resolution` (`"use_existing" | "upload_anyway"`, optional — only meaningful on a duplicate-file retry, see below), `file` (the resume, required)

**Response (`ResumeUploadAcceptedResponse`):**
```json
{
  "resume_id": "uuid", "campaign_candidate_id": "uuid",
  "task_id": "uuid | null",
  "candidate_name_masked": "string", "file_name": "string",
  "campaign_name": "string", "pipeline_stage": "UPLOADED",
  "parse_status": "PENDING"
}
```
`task_id` is `null` only when `resolution=use_existing` resolved a byte-identical duplicate and no new processing was enqueued.

**Special case — exact duplicate file (Epic 3, C2):** if the uploaded file is byte-identical to one already in the system and `resolution` was **not** supplied, this returns **HTTP 409** instead of creating anything, with a structured payload the UI should turn into a "we've seen this file before" dialog offering two choices:
```json
{
  "success": false, "message": "Duplicate file detected.",
  "data": {
    "duplicate_resume_id": "uuid", "candidate_id": "uuid",
    "candidate_name": "string", "uploaded_at": "timestamp",
    "current_pipeline_stage": "string | null",
    "campaign_names": ["string", "..."],
    "original_filename": null,
    "available_resolutions": ["use_existing", "upload_anyway"]
  }
}
```
`original_filename` is always `null` for this response — individual uploads never persist an original filename anywhere. Re-submit the same request with `resolution` set to the user's choice to proceed.

---

### 4.2 `GET /resumes/processing-status/{task_id}` — Poll Processing Status
**Roles:** HR_ADMIN, RECRUITER
**Screen:** post-upload progress indicator

**Purpose:** the original, task-scoped polling endpoint from Epic 1 — given the `task_id` from the upload response, returns the current stage and a per-stage breakdown. **Suggested poll interval: 3–5s while `overall_status` is `QUEUED`/`RUNNING`/`RETRY`; stop polling once it reaches a terminal state.**

**Response (`ResumeProcessingStatusResponse`):**
```json
{
  "task_id": "uuid", "overall_status": "QUEUED | RUNNING | RETRY | SUCCESS | FAILURE | DEAD",
  "current_stage": "string | null", "resume_id": "uuid | null",
  "error_message": "string | null",
  "stages": [ { "stage": "string", "status": "string", "error_message": "string | null", "duration_ms": 812 } ]
}
```

---

### 4.3 `GET /resumes` — Resume List / Search
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Processing History / Resume List

**Purpose:** the general-purpose, paginated resume search across the whole system (or scoped to a campaign) — the backbone of any "all resumes" table view.

**Query params:** `campaign_id`, `parse_status` (`PENDING|PARSING|PARSED|FAILED`), `source` (`individual|bulk`), `email_hash` (exact match only — decrypted name search is **not** offered; searching encrypted PII by plaintext isn't supported anywhere in this codebase), `uploaded_from`/`uploaded_to`, `page`/`size` (max 100), `sort_by` (`created_at|parse_status`), `sort_dir`.

**Response:** `{ items: ResumeListItem[], total, page, size }` — each item: `id, candidate_id, candidate_full_name, candidate_email, file_format, parse_status, version_number, is_active_version, source, bulk_upload_job_id, created_at`.

---

### 4.4 `GET /resumes/{resume_id}` — Resume Detail
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Resume Detail

**Purpose:** the single-resume "everything about this file" view — metadata, the decrypted candidate summary, current processing state, a skill-match summary, embedding status, parser info, and failure detail if applicable. This is the richest single-resource endpoint in the module; most detail screens should be built around it (or its bulk-file twin, §5.5).

**Response (`ResumeDetailResponse`):**
```json
{
  "resume": { "id", "file_path", "file_format", "version_number", "is_active_version", "parse_status", "parser_version", "page_count", "created_at", "bulk_upload_job_id" },
  "candidate": { "id", "full_name", "email", "jurisdiction", "consent_given" },
  "processing": { "task_id", "current_status", "current_stage", "attempt_number", "retry_count" },
  "skill_summary": { "total_skills", "matched", "unmatched", "by_tier": { "EXACT": 8, "ALIAS": 2, "FUZZY": 1 } },
  "embedding_status": { "exists", "embedding_model_version_id", "generated_at" },
  "parser_info": { "parser_used", "parser_version" },
  "failure": { "failed_stage", "error_message", "classification", "moved_to_dlq" } 
}
```
`candidate.full_name`/`candidate.email` are decrypted server-side for this response — never render or log the encrypted/hashed columns, which are not exposed here anyway. `failure` is `null` whenever `parse_status != FAILED`.
**Known gap (flag, don't design around):** for a resume still on its very first processing attempt (never yet succeeded), `processing.task_id`/`current_status` can be unresolvable — see the design doc's Gap 1. If the UI sees `processing.task_id: null` on a resume that visually looks "in progress," that's this known gap, not a bug to report.

---

### 4.5 `GET /resumes/{resume_id}/timeline` — Resume Processing Timeline
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Resume Timeline (often embedded in Resume Detail)

**Purpose:** a stage-by-stage execution trace for one resume's processing attempt — the classic "Upload → Text Extraction → Cleaning → AI Extraction → Skill Normalization → Embedding → Persistence" progress rail. Also usable as a lightweight poll target while a resume is mid-processing (**suggested: 3–5s while any stage is `RUNNING`**).

**Query params:** `attempt_number` (optional — defaults to the latest attempt; pass a specific historical one to compare against a retry).

**Response (`StageTimelineBase` + `resume_id`):**
```json
{
  "task_id": "string", "document_type": "RESUME",
  "overall_status": "QUEUED | RUNNING | RETRY | PAUSED | SUCCESS | FAILURE | DEAD",
  "current_stage": "string | null", "attempt_number": 2, "retry_count": 1,
  "progress_percent": 42.9,
  "queued_at": "timestamp", "started_at": "timestamp | null", "completed_at": "timestamp | null",
  "stages": [ { "stage": "TEXT_EXTRACTION", "status": "SUCCESS", "started_at": "...", "completed_at": "...", "duration_ms": 812, "attempt_number": 1, "error_message": null, "skipped": false, "retryable": true } ],
  "resume_id": "uuid"
}
```
**UI rendering note:** `stages` only ever contains stages that have *actually run* — a stage that hasn't started yet is simply absent from the array (there are no fabricated "pending" placeholder rows). Render the gap between the last real stage and the full expected stage list yourself. There is no separately-tracked "Storage" or file-format "Validation" stage — storage download time is folded into `TEXT_EXTRACTION`, and format validation happens synchronously before any stage runs; render `TEXT_EXTRACTION` as "Text Extraction (includes file download)" rather than inventing a stage that doesn't exist in the data.

---

### 4.6 `GET /resumes/{resume_id}/parse-attempts` — Retry / Attempt History
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Retry History tab

**Purpose:** the full attempt-and-failure history for a resume, merging two underlying sources so a resume that failed *before ever succeeding once* (and therefore has zero rows in the "successful attempts" table) still shows real history instead of an empty tab.

**Response:** `list[ParseAttemptItem]` — `{ source: "parse_attempt"|"stage_failure", attempt_number, stage, parser_used, parser_version, status, error_code, error_detail, confidence_score, duration_ms, occurred_at }`.

---

### 4.7 `GET /resumes/candidate/{campaign_candidate_id}/parsed-json` — Raw Parsed Resume Data
**Roles:** HR_ADMIN, RECRUITER
**Screen:** "View raw extracted data" / debugging panel

**Purpose:** returns the campaign candidate's underlying candidate's currently-active resume's raw AI-extracted `parsed_json` blob verbatim — the same structured data (skills, experience, education, etc.) the scoring pipeline consumes downstream. Useful for a "why did this candidate score X" debugging view, or an editable-fields-review screen.

**Response:** `{ resume_id, candidate_id, parse_status, parsed_json: dict | null }` — `parsed_json` is `null` until `parse_status=PARSED`.

---

### 4.8 `GET /resumes/candidate/{candidate_id}/versions` — Resume Version History *(Epic 3, Phase C1)*
**Roles:** HR_ADMIN, RECRUITER
**Screen:** "Previous resume versions" panel

**Purpose:** every resume version ever uploaded for a candidate (across both individual and bulk paths), most recent first, with exactly one marked `is_active_version: true`. Powers a "this candidate has re-submitted 3 times, here's the history" view.

**Response:** `{ candidate_id, versions: [ { id, version_number, is_active_version, file_format, parse_status, source: "individual"|"bulk", created_at } ] }`. Returns **404** if the candidate has zero resumes at all (not an empty array).

---

### 4.9 `GET /monitoring/queue-status` — Queue Status (Ops)
**Roles:** HR_ADMIN only
**Screen:** Ops / Monitoring Dashboard

**Purpose:** a cheap, database-approximated (not a live broker read) snapshot of how much work is queued/running right now, across both individual and bulk flows. Not recruiter-facing — this is operational/infrastructure detail (queue depth, worker load), not a recruiting concern.

**Query params:** `campaign_id` (optional — omit for platform-wide).
**Response:** `{ resumes_queued, resumes_running, bulk_files_queued, bulk_files_running }`.
**Suggested poll cadence:** 5–10s if kept open on a dashboard; this is a plain `COUNT(*)`, cheap per-call but not free at high concurrent-viewer volume.

---

### 4.10 `GET /monitoring/processing-metrics` — Processing Metrics (Ops)
**Roles:** HR_ADMIN only
**Screen:** Ops / Monitoring Dashboard

**Purpose:** cross-job, cross-resume throughput and failure-rate metrics over a bounded time window (deliberately bounded — an always-fresh unbounded aggregate would need a background pre-aggregation job, which was explicitly ruled out).

**Query params:** `window` (`"1h" | "24h" | "7d"`, default `24h`).
**Response:** `{ window, throughput_per_hour, avg_duration_by_stage: {stage: ms}, failure_rate_by_stage: {stage: rate}, top_failure_reasons: [{exception_type, count}] }`.
**Suggested poll cadence:** 60s+ — this is an aggregate, not a live number; don't poll it like a progress bar.

---

## 5. Epic 2 (M05-E02) — Bulk ZIP Upload

### 5.1 `POST /bulk-uploads` — Upload a Bulk ZIP
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload modal

**Purpose:** upload a ZIP archive containing many resumes for one campaign. Every file inside inherits the same jurisdiction and the same one-time consent confirmation. Validates and stores the ZIP, creates the job record at `PENDING`, and enqueues background extraction — nothing about individual files is known yet (bulk uses a "parse-first" architecture: identity is only discovered once each file's AI extraction succeeds).

**Request:** multipart form — `campaign_id`, `jurisdiction` (default `GLOBAL`), `consent_confirmed` (bool, required), `file` (the ZIP).
**Response (`BulkUploadAcceptedResponse`):** `{ bulk_upload_job_id, task_id, campaign_name, original_filename, status: "PENDING" }`.
**Rejected up front (before any storage write):** non-ZIP content, ZIP over `ZIP_MAX_SIZE_MB`, more than `MAX_FILES_PER_ZIP` real files (200), a paused/closed campaign.

---

### 5.2 `GET /bulk-uploads` — Bulk Upload History List
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload Dashboard (job list)

**Purpose:** paginated list of past/in-progress bulk jobs for one campaign, most recent first — the landing view for "show me every ZIP I've uploaded to this campaign."

**Query params:** `campaign_id` (required), `page`, `size`.
**Response:** `{ total, page, size, items: [ { id, original_filename, status, total_files, queued_count, processed_count, failed_count, duplicate_count, created_at, completed_at } ] }`.

---

### 5.3 `GET /bulk-uploads/export` — Export Job History
**Roles:** HR_ADMIN, RECRUITER
**Screen:** "Export" button on the Bulk Upload Dashboard

**Purpose:** the same history as §5.2 but unpaginated and rendered as a real `.xlsx` file — a `StreamingResponse`, not the usual JSON envelope. Registered before `/{bulk_upload_job_id}` in the route table so `"export"` is never swallowed as a job-id path parameter.

**Query params:** `campaign_id` (required).
**Response:** raw `.xlsx` bytes, `Content-Disposition: attachment`.

---

### 5.4 `GET /bulk-uploads/{id}/progress` — Live Progress + ETA *(Epic 4, Phase D4)*
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload progress bar

**Purpose:** a lightweight, frequent-polling-safe endpoint purpose-built for a live progress bar — deliberately separate from §5.6's full job detail, which also returns the entire unpaginated file list and is too heavy to poll every few seconds.

**Response (`BulkUploadProgressResponse`):**
```json
{
  "bulk_upload_job_id": "uuid", "status": "PROCESSING",
  "total_files": 50, "processed_count": 30, "failed_count": 3, "duplicate_count": 2,
  "remaining_count": 15,
  "percent_complete": 70.0,
  "estimated_completion_at": "timestamp | null"
}
```
`percent_complete` is clamped to a max of `100.0` (a display guard, never the source of truth — the raw counters are). `estimated_completion_at` is `null` until at least one file has resolved; once populated it's a simple linear extrapolation, not a model — treat it as a rough estimate in the UI copy ("about 4 minutes remaining"), not a promise.
**Suggested poll cadence:** 5–10s while `status` is `EXTRACTING`/`PROCESSING`; stop once terminal.

---

### 5.5 `GET /bulk-uploads/{id}/file-log` — Live Per-File Log *(Epic 4, Phase D5)*
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload "live activity feed"

**Purpose:** a scrolling, most-recently-resolved-first log of every file that has reached a terminal outcome — the "what just happened" feed a UI shows next to the progress bar. Unlike the progress bar, this keeps working (and stays meaningful) after the job finishes, since it's a plain read of already-durable rows.

**Query params:** `limit` (default 50, max per `MAX_PAGE_SIZE`), `offset`.
**Response (`BulkUploadFileLogResponse`):**
```json
{
  "entries": [ { "filename": "resume_12.pdf", "result": "SUCCESS | FAILED | DUPLICATE | SKIPPED", "reason": "string | null", "timestamp": "..." } ],
  "total": 47, "limit": 50, "offset": 0
}
```
`reason` is always populated for `FAILED`/`SKIPPED` (never a bare null with no explanation) and always `null` for `SUCCESS`/`DUPLICATE`. This is the one place `"DUPLICATE"` appears as its own clean badge value for bulk files (the underlying `BulkUploadFileStatus` itself has no such value — see §2).
**Suggested poll cadence:** 5–10s while the job is active, same as the progress bar — pair the two calls together on one timer.

---

### 5.6 `GET /bulk-uploads/{id}` — Full Job Detail
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload Details page (header + file table)

**Purpose:** the "everything about this one job" view — job-level counters plus the complete (unpaginated) per-file list. Fine for small-to-medium jobs; for a large ZIP, use §5.7's paginated file grid instead of relying on this endpoint's embedded array.

**Response (`BulkUploadJobDetailResponse`):**
```json
{
  "id", "campaign_id", "uploaded_by", "original_filename", "status",
  "consent_confirmed", "total_files", "queued_count", "processed_count",
  "failed_count", "duplicate_count", "error_summary", "created_at", "completed_at",
  "files": [ { "id", "original_filename", "status", "task_id": "string | null", "retry_count": "int | null" } ]
}
```
`files[].task_id` (added ad-hoc, mid-Epic-4) lets the UI deep-link straight from this list into §5.8's per-file timeline without a second lookup — `null` only for a file not yet dispatched to its own per-file task.

---

### 5.7 `GET /bulk-uploads/{id}/files` — Paginated File Grid
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload Details (large-job file table)

**Purpose:** the real answer to "list every file in this job" for a ZIP too large to render unpaginated — filterable, searchable, sortable. This is additive to §5.6, not a replacement; that endpoint's embedded array is left exactly as-is.

**Query params:** `status` (`QUEUED|RUNNING|PROCESSED|FAILED|CANCELLED`), `search` (matches `original_filename`, a plaintext column — safe to free-text search, unlike candidate names), `page`, `size`, `sort_by` (`created_at|status|original_filename`), `sort_dir`.
**Response:** `{ items: [ { id, original_filename, status, task_id, retry_count, created_at } ], total, page, size }`.

---

### 5.8 `GET /bulk-uploads/{id}/files/{file_id}` — Single File Detail
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk File Detail

**Purpose:** mirrors §4.4's Resume Detail shape exactly, for one file inside a bulk job — with one structural difference: `resume`/`candidate` are `null` until identity actually resolves, since a file that fails before AI extraction succeeds never gets a `Resume` row at all (the parse-first architecture).

**Response (`BulkFileDetailResponse`):** same shape as `ResumeDetailResponse` (§4.4) plus `file_id`, `bulk_upload_job_id`, `original_filename`, `file_status`, `task_id` at the top level; `resume`/`candidate`/`skill_summary`/`embedding_status`/`parser_info` are all nullable.

---

### 5.9 `GET /bulk-uploads/{id}/files/{file_id}/timeline` — Single File Timeline
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk File Timeline

**Purpose:** identical `StageTimeline` shape as §4.5's resume timeline, for one file. **No data-availability gap here** (unlike the individual-resume timeline) — `bulk_upload_job_files.task_id` is populated at row-creation time, before any processing starts, so this resolves reliably at every point in a file's life, including its very first moment as `QUEUED`.

**Query params:** `attempt_number` (optional).
**Response:** `StageTimelineBase` + `file_id` — identical fields to §4.5.

---

### 5.10 `GET /bulk-uploads/{id}/metrics` — Job Aggregate Metrics
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Bulk Upload Dashboard (per-job metrics card)

**Purpose:** stage-level aggregate performance for one job — how long each stage typically takes, how often it needed a retry, overall success rate. Good for a "this job's health at a glance" card next to the job-list row.

**Response (`BulkJobMetricsResponse`):** `{ bulk_upload_job_id, total_files, processed, failed, duplicate, avg_duration_by_stage: {stage: ms}, retry_rate, success_rate }`.
**Suggested cache/poll:** short TTL (30–60s) while the job is `PROCESSING` — this is an aggregate, doesn't need millisecond freshness, but a dashboard left open shouldn't re-run the same `GROUP BY` every poll either.

---

### 5.11 `GET /bulk-uploads/{id}/failures` — Failed-File Triage List
**Roles:** HR_ADMIN, RECRUITER
**Screen:** Failure Detail / triage view

**Purpose:** every failed file in one job with its failure reason, in a single call — the list a support/ops person works through to decide what's worth replaying (§5.12) vs. a genuine bad file.

**Query params:** `page`, `size`.
**Response:** `{ items: [ { file_id, original_filename, failed_stage, error_message, classification: "TRANSIENT"|"PERMANENT"|"UNKNOWN", retry_count, failed_at } ], total, page, size }`. `classification` is the single most useful field for deciding whether a replay is worth trying — `TRANSIENT` failures are good replay candidates, `PERMANENT` ones usually aren't.

---

### 5.12 `POST /bulk-uploads/{id}/files/{file_id}/replay` — Replay One Failed File
**Roles:** HR_ADMIN only
**Screen:** a "Replay" button on a failed file row (§5.11 or §5.7)

**Purpose:** re-enqueues a single dead-lettered file's parse task under a fresh `task_id`. Only files that actually reached the dead letter queue (retries exhausted) are replayable — a deterministic failure like a duplicate-candidate outcome never dead-letters and has nothing here to replay.

**Response (`BulkUploadFileReplayResponse`):** `{ file_id, bulk_upload_job_id, original_filename, status, new_task_id }`.

---

### 5.13 `POST /bulk-uploads/{id}/cancel` — Cancel a Job
**Roles:** HR_ADMIN, RECRUITER
**Screen:** "Cancel" button on an in-progress job

**Purpose:** cancels a bulk job that hasn't finished yet — the job moves to `CANCELLED` and every still-`QUEUED` file is bulk-cancelled with it. A file whose per-file task is *already running* is left to finish naturally (no real Celery-level task revocation exists in this codebase — this mirrors the same cooperative cancellation the campaign-pause feature already uses).

**Response (`BulkUploadCancelResponse`):** `{ bulk_upload_job_id, status: "CANCELLED", files_cancelled: int }`.

---

## 6. Epic 3 (M05-E03) — Duplicate Detection & Validation

Epic 3's HTTP-facing additions are §4.1's duplicate-file 409 (C2, individual), §4.8 (C1, versions), and the two below. C3 (bulk exact-duplicate handling) and C4 (resubmission alerting) added **no new HTTP surface** — C3 extends the existing bulk per-file task's internal logic (reflected in §5.5's `"DUPLICATE"` result and §5.2's `duplicate_count`), and C4 is a background detection sweep with no UI-facing endpoint.

### 6.1 `POST /campaign-candidates/{id}/update-resume` — Resubmit a Resume *(Phase C5)*
**Roles:** HR_ADMIN, RECRUITER — **but see below, this is stage-gated, not just role-gated**
**Screen:** "Update Resume" action on an existing candidate's scorecard

**Purpose:** the resolution path for "this candidate already exists in this campaign" — instead of erroring out permanently, this lets the recruiter/HR admin upload a new resume version for the *same* campaign+candidate pairing, resetting all evaluation state (scores, AI results, rejection/override flags) and re-triggering the full processing pipeline from scratch.

**Whether this is allowed at all — and whether it needs HR_ADMIN specifically — depends on the candidate's current `pipeline_stage`**, not on role alone: a `RECRUITER` can trigger it freely before `SHORTLISTED`; once a candidate has passed `SHORTLISTED` (`HOLD`/`HM_REVIEW`/`INTERVIEW`), only `HR_ADMIN` can, and only with a `reason`. This is enforced server-side by the same `PipelineTransitionService`/`allowed_transitions` mechanism used elsewhere, not hand-rolled per-endpoint logic — a UI should treat a 409 here as "this transition isn't allowed from the candidate's current stage," not as a generic error.

**Request:** multipart form — `reason` (optional/required depending on stage, see above), `file` (the new resume).
**Response (`UpdateResumeResubmissionResponse`):** `{ campaign_candidate: CampaignCandidateResponse, new_resume_id, task_id }`.

**How the UI normally reaches this endpoint:** §4.1's `POST /resumes` upload flow can itself return a 409 for "candidate already exists in this campaign" (a separate, pre-existing check, not the duplicate-*file* 409 in §4.1) — that 409's `data` payload is a `ResubmissionInfoResponse`:
```json
{ "campaign_candidate_id", "current_pipeline_stage", "current_resume_id", "can_update_resume": true, "requires_hr_confirmation": false }
```
Use `can_update_resume`/`requires_hr_confirmation` to decide whether to offer this endpoint as the next step, and whether to prompt for a mandatory `reason` first.

---

### 6.2 `GET /candidates/{candidate_id}/campaign-history` — Cross-Campaign History *(Phase C6)*
**Roles:** HR_ADMIN only
**Screen:** "This candidate elsewhere" panel

**Purpose:** every campaign a given candidate has ever been submitted to, most recent first, each with its own independent score/stage — the view that answers "has this person applied before, and how did that go?" without leaking one campaign's scoring into another's (score isolation was explicitly verified, not just assumed).

**Response (`CandidateCampaignHistoryResponse`):**
```json
{
  "candidate_id": "uuid", "total_campaigns": 2,
  "history": [
    { "campaign_candidate_id", "campaign_id", "campaign_name", "jd_title", "submission_date",
      "pipeline_stage": "SELECTED", "composite_score": 91.5, "outcome": "Selected" }
  ]
}
```
`outcome` is a derived, UI-friendly tri-state (`"Selected" | "Rejected" | "In Progress"`) — always prefer it over trying to derive the same thing from `pipeline_stage` yourself, since the mapping isn't 1:1 (e.g. `HOLD`/`HM_REVIEW`/`INTERVIEW` are all `"In Progress"`).

---

## 7. Epic 4 (M05-E04) — Upload Progress & Tracking

Several Epic 4 phases (D1, D2) added **no new endpoint** — they extended two pre-existing, heavily-used endpoints. Those two are documented here since a UI built against Resume Intake will already be calling them.

### 7.1 `GET /campaign-candidates/campaign/{campaign_id}` — Candidate List *(extended by Phase D1)*
**Screen:** Campaign candidate list / pipeline board

**What Epic 4 added:** a `parse_status: "PENDING"|"PARSING"|"PARSED"|"FAILED"|null` field on every row, read live off the linked `Resume` — this is what lets a candidate list show a live "still processing…" badge next to each name without a separate call per candidate. **Poll this list on whatever cadence the candidate-list screen already uses** — no dedicated polling endpoint was built for this, since the existing list call already returns everything needed.
`parse_status` is independent of `pipeline_stage` on the same row — see §2's state-machine note; don't assume one changes the other.

---

### 7.2 `GET /campaign-candidates/{campaign_candidate_id}` — Candidate Scorecard *(extended by Phase D2)*
**Screen:** Candidate Scorecard detail page

**What Epic 4 added:** a `processing_timeline: ProcessingTimelineEntry[]` array — every `celery_task_log` row for this candidate's processing, oldest first:
```json
{ "task_type": "string", "status": "string", "queued_at": "...", "started_at": "... | null", "completed_at": "... | null", "duration_display": "1m 42s | null", "error_message": "string | null" }
```
This is a **read-only history strip**, not a retry control — it deliberately does not tell you whether a `DEAD` entry is retryable (that judgment call belongs entirely to §7.6's retry/replay endpoints, which resolve it correctly using the dead letter queue rather than guessing from this timeline).

---

### 7.3 `GET /bulk-uploads/{id}/progress` and `GET /bulk-uploads/{id}/file-log`
Already documented in full at §5.4 and §5.5 — listed here again only as a pointer, since both are Epic 4 (D4/D5) deliverables and belong conceptually with the rest of this section.

---

### 7.4 `GET /campaigns/{campaign_id}/upload-history` — Unified Upload History *(Phase D7)*
**Roles:** HR_ADMIN, RECRUITER
**Screen:** "All uploads for this campaign" combined timeline

**Purpose:** individual resume uploads and bulk ZIP uploads have always lived in two completely separate tables with two separate history views (§5.2 for bulk; nothing dedicated for individual). This endpoint merges both into one chronological, filterable feed — the single screen a recruiter opens to answer "show me everything uploaded to this campaign, in order, regardless of how it came in."

**Query params:** `uploaded_by`, `date_from`/`date_to`, `upload_type` (`"individual" | "bulk"`), `outcome`, `limit` (max per `MAX_PAGE_SIZE`), `offset`.

**Response (`UnifiedUploadHistoryResponse`):**
```json
{
  "entries": [
    {
      "upload_type": "individual", "filename": null, "uploaded_by": "string", "uploaded_by_name": "string | null",
      "created_at": "...", "outcome": "string",
      "resume_id": "uuid", "parse_status": "PARSED", "pipeline_stage": "SCREENING",
      "bulk_upload_job_id": null, "total_files": null, "processed_count": null, "failed_count": null, "duplicate_count": null, "status": null
    }
  ],
  "total": 12, "limit": 50, "offset": 0,
  "available_uploaders": [ { "user_id": "string", "full_name": "string | null" } ]
}
```
**Discriminated by `upload_type`** — on an `"individual"` row, only `resume_id`/`parse_status`/`pipeline_stage` are populated (the bulk-only fields are always `null`); on a `"bulk"` row it's the reverse. **`filename` is always `null` on individual rows** — this is an honest, permanent data gap (no table anywhere stores an individual upload's original filename), not a bug. **`available_uploaders`** is derived from the campaign's *entire* upload history regardless of whatever filter is currently applied — always populate an "uploaded by" filter dropdown from this field, never from the (possibly filtered) `entries` array itself, or the dropdown will shrink every time a filter is applied.

---

### 7.5 `GET /monitoring/upload-queue-dashboard` — Platform-Wide Upload Queue Dashboard *(Phase D12)*
**Roles:** HR_ADMIN only
**Screen:** Ops / Monitoring Dashboard (platform-wide section)

**Purpose:** one snapshot answering "how healthy is upload processing right now, across the whole platform, not just one campaign" — pending/queued/running/dead counts, the two upload-critical external services' circuit-breaker health, and a ranked per-campaign breakdown so an ops user can immediately spot which campaign is actually causing a backlog.

**Response (`UploadQueueDashboardResponse`):**
```json
{
  "generated_at": "2026-07-30T06:00:41Z",
  "pending_resumes_count": 25, "resumes_queued": 1, "resumes_running": 3,
  "processing_bulk_jobs_count": 4, "dead_tasks_count": 2,
  "circuit_breakers": [ { "service_name": "SUPABASE_STORAGE", "state": "CLOSED", "failure_count": 0, "opened_at": null } ],
  "any_circuit_breaker_open": false,
  "campaign_breakdown": [
    { "campaign_id": "uuid", "campaign_name": "string", "pending_resumes_count": 3, "queued_resumes_count": 0, "queue_depth": 3 }
  ]
}
```
**Field notes for the UI:**
- `generated_at` — always render this next to the dashboard so the user knows how fresh the snapshot is; this is a live, on-demand query, not a cached pre-aggregation.
- `dead_tasks_count` is scoped to exactly the two resume-intake task types (individual + bulk-per-file) — it will **not** include dead tasks from unrelated pipelines (e.g. JD processing), by design.
- `circuit_breakers` **only lists a service once it has failed at least once** — a service that has never failed simply doesn't appear in the array at all (there is no synthetic "CLOSED, never failed" placeholder row). Treat an absent entry the same as a `CLOSED` one in the UI.
- `campaign_breakdown` is capped (currently at 20 campaigns) and sorted by `queue_depth` descending; when two campaigns tie on `queue_depth`, the tie is broken deterministically by `campaign_name` ascending — the ordering is stable and reproducible, safe to render without the UI re-sorting itself.
- `any_circuit_breaker_open` is a convenience boolean for a top-level "⚠ degraded" banner — flip the banner on this field alone rather than iterating `circuit_breakers` yourself.
**Suggested poll cadence:** 15–30s for an always-open ops dashboard — this is a real-time-ish operational view, but every field is a live query, so avoid sub-5s polling with many concurrent viewers.

---

### 7.6 `POST /resumes/{resume_id}/retry` and `POST /resumes/dead-letter-queue/{dlq_id}/replay` — Retry / Replay *(Phase D10)*
**Roles:** HR_ADMIN only
**Screen:** "Retry" action on a `FAILED` resume (Resume Detail, §4.4) or a dead-lettered entry (an ops-facing DLQ view, not yet built as its own screen)

**Purpose:** two related but distinct recovery actions for an individual resume upload, mirroring the bulk-file replay pattern (§5.12) that already existed for bulk but had no individual-upload equivalent before this phase:

- **`POST /resumes/{resume_id}/retry`** — re-dispatches processing for a resume that is currently `parse_status=FAILED`, using its already-stored file (no re-upload needed). Rejects with **409** if the resume isn't currently `FAILED`, or if it has no campaign linked at all (needed to resolve which AI prompt template to use — see below).
- **`POST /resumes/dead-letter-queue/{dlq_id}/replay`** — replays a specific dead-lettered failure (retries already exhausted, sitting in the dead letter queue) by its DLQ entry id rather than by resume id. Rejects with **409** if that entry was already replayed once, or if it isn't a resume-processing entry at all (the same dead letter queue table is shared with other pipelines — e.g. a JD-processing failure's DLQ entry is not replayable here).

**Both return the same shape (`ResumeRetryResponse`):** `{ resume_id, task_id, parse_status: "PENDING" }` — treat a successful call exactly like a fresh upload's processing kickoff: start polling §4.2/§4.5 with the returned `task_id`.

**Why a resume needs a linked campaign to retry at all:** re-processing needs to know which AI prompt template to use, which is a property of the campaign, not the resume itself. If a resume was somehow never linked to any campaign, there is nothing to resolve this from — the endpoint fails clean with a 409 rather than crashing mid-dispatch. In practice every resume from either upload path is always linked to at least one campaign, so this is a defensive edge case, not something the UI needs to design a whole flow around.

---

## 8. Compliance (cross-epic) — GDPR-style Erasure

### `DELETE /candidates/{candidate_id}` — Erase Candidate
**Roles:** HR_ADMIN only
**Screen:** an explicit, deliberate "Delete Candidate" admin action — not exposed as a casual list-row action

**Purpose:** permanently deletes a candidate and every row that references them — resumes (including the stored files themselves), scores, skills, embeddings, pipeline/campaign history, consent records, and email notifications — regardless of whether the candidate came from an individual or a bulk ZIP upload. The only surviving trace afterward is the single `audit_log` entry this call itself writes. This is irreversible; the UI should gate it behind a real confirmation step, not a single click.

**Query params:** `reason` (optional, max 500 chars — recorded on the audit entry).
**Response:** `{ success: true, message: "...", data: null }` — nothing to render, just confirm success and remove the candidate from any cached UI state.

---

## 9. Quick UI-screen → endpoint map

| Screen | Primary endpoint(s) |
|---|---|
| Upload modal (individual) | §4.1 → poll §4.2 or watch §7.1's list |
| Upload modal (bulk ZIP) | §5.1 → poll §5.4 + §5.5 together |
| Candidate list / pipeline board | §7.1 (list with live `parse_status`) |
| Candidate Scorecard | §7.2 (scorecard + processing timeline), §6.1 (resubmit action) |
| Resume Detail | §4.4 (detail), §4.5 (timeline), §4.6 (retry history), §7.6 (retry action) |
| Resume List / Processing History | §4.3 |
| Bulk Upload Dashboard | §5.2 (job list), §5.10 (per-job metrics), §5.4/§5.5 (per-job live progress) |
| Bulk Upload Details | §5.6 (header + small file list) or §5.7 (paginated grid for large jobs), §5.13 (cancel) |
| Bulk File Detail / Timeline | §5.8, §5.9 |
| Failure Detail / triage | §5.11 (list), §5.12 (replay) |
| Campaign upload history (combined) | §7.4 |
| "This candidate elsewhere" panel | §6.2 |
| Ops / Monitoring Dashboard | §4.9, §4.10, §7.5 (platform-wide, HR_ADMIN only) |
| Delete Candidate (admin) | §8 |

---

## 10. What's intentionally *not* here yet

Per the implementation log, these Epic 4 phases have not been built and therefore have no endpoints to document: **D3** (deferred — no AI-evaluation task exists yet to build against), **D8** (Upload History Export), **D9** (Failed Uploads Aggregation View), **D11** (Persistent Failure Notification — email), **D13** (Platform Bottleneck Alerting), **D14** (Platform Upload Metrics Export). If a UI mock or design references any screen backed by one of these, flag it — there is genuinely no API yet, this isn't a documentation gap.
