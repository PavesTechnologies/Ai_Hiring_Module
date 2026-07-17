# The JD Upload Retry System, Explained for Everyone

This document explains a feature built into the system that processes uploaded Job
Description (JD) documents. It's written so that anyone at the company — technical or
not — can understand what happens when a JD upload runs into trouble, and why it was
built the way it was. Technical readers get exact file names, table names, and
numbers throughout; a full technical reference is also collected at the end in
Section 9.

---

## 1. What problem does this solve?

When someone uploads a job description, the system doesn't process it in one single
step — it works through a series of steps, one after another (more on those steps in
Section 2). One of those steps calls out to an AI service (Google's Gemini) to read
the document and pull structured information out of it — job title, requirements,
skills, and so on.

Before this feature existed, if *any* step failed partway through — say, a network
hiccup, or the AI service being briefly unavailable — the entire upload had to be
processed from scratch, starting again at step one. That's wasteful in an obvious
way (time), but it's especially wasteful for the AI step specifically: re-running the
whole thing from the beginning means calling the AI service again, which costs real
money and API usage, even though the AI step might have already completed
successfully the first time and it was some *later* step that actually failed.

What changed: the system now remembers exactly where it stopped. If a document gets
through several steps successfully and then fails on, say, step 6, the next attempt
doesn't redo steps 1 through 5 — it picks up right where it left off, at step 6. Nothing
that already succeeded gets redone, and nothing that costs money (like the AI call)
gets paid for twice unless that specific step is the one that actually failed.

---

## 2. What does "resume from the failed stage" actually mean?

Think of processing a JD upload like grading a nine-part assignment, where each part
has to be done in order because later parts depend on earlier ones. In this system,
each "part" is called a **stage** — one distinct piece of work the document goes
through before it's considered fully processed. Note that later stages depend on the
output of earlier ones, which is exactly why they must run in order. Here is the
actual sequence, in plain terms, as it exists in the code today:

1. **VALIDATION** — check the upload itself is well-formed and acceptable.
2. **STORAGE** — save the uploaded file somewhere durable.
3. **TEXT_EXTRACTION** — pull the raw text out of the file (e.g. out of a PDF or Word
   document).
4. **TEXT_CLEANING** — tidy up that raw text (removing junk formatting, etc.) so it's
   ready to hand to the AI.
5. **AI_EXTRACTION** — send the cleaned text to the Gemini AI service and get back a
   structured breakdown (job title, requirements, skills, etc.). This is the
   expensive, "costs money" step referenced in Section 1.
6. **JSON_VALIDATION** — check that what the AI handed back is actually well-formed
   and usable.
7. **SKILL_NORMALIZATION** — match the skills the AI found against the system's own
   standard skill list, so "JS" and "JavaScript" are recognized as the same thing.
8. **EMBEDDING_GENERATION** — generate a numeric representation of the document used
   later for search/matching.
9. **PERSISTENCE** — save the final, fully-processed job description into the
   system for real.

If, say, step 5 (AI_EXTRACTION) fails, the system saves a copy of everything steps 1
through 4 already produced — the stored file, the extracted text, the cleaned text —
before giving up on that attempt. The next time it tries, it skips straight past
steps 1–4 (since their output is already saved) and starts again at step 5. It never
redoes 1 through 9 from scratch; it only ever redoes from the point of failure
onward.

**Technical detail:** the stage list above is `ProcessingStage` in
`app/models/async_tasks.py`, and the actual execution order used for skip decisions is
`STAGE_ORDER` in `app/services/document_processing/retry_policy.py` — a separate,
explicit list rather than relying on the order the stages happen to be declared in
(see Section 6 for why that distinction matters).

---

## 3. When does this actually kick in? (trigger conditions)

In plain terms: this system only does anything when a step actually fails — throws
an error. If every step succeeds, this whole mechanism stays completely dormant (see
Section 5). The moment a step does fail, three things need to be decided: should this
be tried again, how soon, and how many times before the system stops trying and asks
a person to look at it instead?

Not every failure is worth retrying. A **temporary network hiccup** talking to the
AI service is worth retrying — try again in a few seconds and it'll probably work.
But if the document's content is fundamentally broken (e.g. the AI came back with a
response that doesn't parse as valid data at all), retrying the exact same broken
input isn't going to fix anything — trying again five more times would just fail five
more times. The system tries to tell these two situations apart automatically:

- A failure judged **worth retrying** ("temporary/transient") gets tried again
  automatically, after a short wait.
- A failure judged **not worth retrying** ("permanent") is immediately set aside for
  a human to look at — no further automatic attempts.
- A failure the system **isn't sure how to categorize** ("unknown") is, today, still
  retried like a temporary one, up to the attempt limit — it's just not confidently
  classified either way.

If a *temporary* failure keeps happening even after several retries, the system
eventually gives up on that one too and hands it off to a human — it doesn't retry
forever.

**How many retries, and how long between them:** every stage gets up to 3 attempts by
default before giving up, except AI_EXTRACTION specifically, which gets up to 5
attempts — because that step is the one most likely to hit a brief hiccup calling an
external service, and it's the most expensive step to have to abandon. Waiting time
between attempts doubles each time (starting around 5–10 seconds and capping out
around 1–2 minutes), so the system doesn't hammer a struggling service repeatedly in
quick succession.

**Technical detail:** the classification logic is `classify()` in
`app/services/document_processing/error_classifier.py`. Today it recognizes
`ConnectionError`/`TimeoutError` as `TRANSIENT` (worth retrying) and
`ValueError`/`KeyError`/`TypeError` as `PERMANENT` (not worth retrying); anything else
falls into `FailureClassification.UNKNOWN`. The per-stage retry limits and wait times
are `STAGE_POLICIES`/`DEFAULT_POLICY` in `retry_policy.py`:
`DEFAULT_POLICY = max_attempts=3, base_delay=5s, cap=60s`, and
`AI_EXTRACTION → max_attempts=5, base_delay=10s, cap=120s`, with the wait time doubling
each attempt (`compute_backoff_seconds`) up to the cap. **Note: this differs from
ideal** — the classifier does not yet recognize the actual error types the Gemini AI
service raises for things like rate-limiting, so genuinely-temporary AI-service
failures often land in `UNKNOWN` rather than being confidently marked `TRANSIENT` (see
Section 7).

---

## 4. Step-by-step: what happens when something fails

### (a) A failure that gets retried and eventually succeeds

*The story:* The system is reading a job description that was just uploaded. It saves
the file, pulls out the text, cleans it up — all fine so far. Then it sends the
cleaned text to the AI service to extract the structured details, but the AI service
happens to be having a brief connectivity issue right at that moment, and the request
fails.

1. The system notices this step failed, and writes down two things: a note that "this
   attempt failed at the AI-extraction step, here's the error," and a saved snapshot
   of everything produced so far (the stored file, the extracted text, the cleaned
   text) — so nothing already done gets lost.
2. It looks at the type of failure and decides: this looks like a temporary
   connectivity problem, worth trying again.
3. Since this is only the first failed attempt (and up to 5 are allowed for this
   step), it schedules another try in about 10 seconds rather than giving up.
4. On the next attempt, the system checks: is there a saved snapshot for this
   document? Yes — so it restores everything from that snapshot instead of
   re-reading the file and re-cleaning the text from scratch, and picks up directly
   at the AI-extraction step.
5. This time, the AI service responds successfully. The remaining steps
   (checking the AI's response, matching skills, generating search data, and finally
   saving the finished job description) all complete normally.
6. Because the whole thing finished successfully this time, the system throws away
   the saved snapshot from step 1 — it's no longer needed.

*Technical detail:* the failing stage raises an exception, which
`StageExecutionService.run_stage()` (in
`app/services/document_processing/stage_execution_service.py`) catches; it marks that
stage's execution row `FAILED`, and — because a serializable context and a checkpoint
repository were passed in — writes a `DocumentProcessingCheckpoint` row
(`task_id`, `failed_at_stage=AI_EXTRACTION`, `context_data` = the full serialized
in-memory pipeline state) via `CheckpointRepository.upsert()`, then re-raises as a
`StageExecutionError`. The Celery task `process_jd_document`
(`app/tasks/jd_processing_tasks.py`) catches that and calls `RetryDriver.handle_failure()`
(`app/services/document_processing/retry_driver.py`), which classifies the error,
records a `StageFailureLog` row, and — since it's `TRANSIENT` and attempt 1 of 5 —
calls `celery_task.retry(countdown=10, max_retries=None)`. On redelivery,
`JDProcessingPipeline.run()` (`app/services/jd/jd_processing_pipeline.py`) finds the
checkpoint, restores the context via `context_serializer.from_dict()`, marks every
stage before `AI_EXTRACTION` as `SKIPPED` (no recomputation), and re-runs from
`AI_EXTRACTION` onward. On overall success, `process_jd_document` deletes the
checkpoint row.

### (b) A failure that's given up on and handed to a human

*The story:* The system is checking the AI service's response to make sure it's
usable, but the response is fundamentally malformed — this isn't a "try again later"
kind of problem, it's a "this input is broken" kind of problem.

1. The system writes down the same two things as before: what failed and where, plus
   a saved snapshot of everything produced up to that point.
2. It looks at the type of failure and this time decides: this isn't the kind of
   problem that trying again will fix.
3. Instead of scheduling another attempt, it immediately sets the document aside in a
   holding area for problems that need a person's attention, along with the error
   message and the saved snapshot of progress (in case someone needs to investigate
   or manually pick up from there later).
4. The upload is marked as failed. No further automatic attempts happen.

*Technical detail:* the stage raises (e.g.) a `ValueError`, wrapped the same way as
in scenario (a) into a `StageExecutionError`, with the same checkpoint write.
`error_classifier.classify()` maps `ValueError` to `PERMANENT`. In
`RetryDriver.handle_failure()`, a classification of `PERMANENT` short-circuits
straight to giving up — **regardless of how many attempts have been used so far**;
it doesn't wait until the attempt limit is reached. It fetches the checkpoint (for its
saved context), creates a `DeadLetterQueue` row (`original_task_id`, `task_type`,
`final_error_message`, `input_payload` = the checkpoint's saved context, `retry_count`,
timestamps), and returns without calling `.retry()`. `process_jd_document` then calls
`task_log_service.mark_failure()` and re-raises the original exception so Celery
records the task as failed. The checkpoint row is deliberately *not* deleted at this
point (see Section 6).

---

## 5. What happens when everything works fine?

If a document sails through every single step without any failure, this entire
mechanism does nothing extra. No "failed attempt" note is written, no saved snapshot
is created, no holding-area entry is made. It's a safety net that only activates when
something actually goes wrong — it is not something that adds overhead to every
normal upload.

**Technical detail:** the checkpoint-write and failure-log-write code paths both live
exclusively inside the `except` (failure) branch of `StageExecutionService.run_stage()`.
On a fully successful run, zero rows are ever written to
`document_processing_checkpoints` or `stage_failure_logs` — the only records produced
are the normal per-stage `SUCCESS` rows in `document_processing_stage_executions` and
a `SUCCESS` row in `celery_task_log`, exactly as if this mechanism didn't exist. The
same is true for the duplicate-document-detection path (where the system recognizes
an identical JD was already processed and skips the remaining steps) — that path also
writes zero checkpoint or failure-log rows.

---

## 6. Why was it built this way?

**Why does the system need to know it's "the same document" across retries?**
Every upload gets a unique ID the moment it's submitted — before any processing
starts. Every retry of that same upload reuses that exact same ID. This is what lets
the system connect "the attempt that just failed" with "the saved snapshot from
before" and "the retry that's about to happen" — without it, the system would have no
way to know a retry belongs to the same document rather than being a brand new
upload. Rather than invent a separate tracking number, the system reuses the ID
already generated for the upload itself, since it already uniquely identifies "this
one upload" from the very first moment it's accepted.

**Why save everything done so far, not just a note that "it failed here"?**
When the system resumes, it needs to actually pick up mid-process — which means it
needs everything the earlier steps produced, not just a note about which step broke.
If it only remembered *where* it failed without saving *what came before*, it would
have no choice but to redo those earlier steps anyway to get that data back — which
defeats the entire point of resuming instead of restarting.

**Why does the system decide for itself when to give up, rather than retrying
forever?** Retrying forever would mean a genuinely broken document (or a genuinely
down external service) could sit there endlessly consuming resources and never
actually resolve. By deciding — in the system's own logic, not left up to whatever the
underlying task-queue software happens to do by default — exactly how many times to
try and when to stop, there's one single, predictable place that governs "how many
chances does this get" per step, instead of that decision being split across two
different systems that might disagree.

**Why keep a failed document's saved progress around even after it's handed off to a
human, instead of deleting it?** The saved snapshot represents real, valid work
(everything that succeeded before the failure) — deleting it the moment something is
handed off for manual review would throw away that value. Keeping it around means
that if the system ever gains the ability to automatically "replay" a resolved
problem later, or if someone manually investigating wants to see exactly what state
the document was in at the point of failure, that information is still there. (No
such automatic replay feature exists today — see Section 7 — but the data is kept
specifically so it could be built later without needing to reprocess the document
from scratch.)

---

## 7. What does NOT this system handle?

Being upfront about the current limits:

- **This only covers Job Description uploads.** There's no equivalent system yet for
  candidate resumes — that pipeline doesn't exist in the codebase today. (The
  underlying data was built in a way that could support it later, but no resume
  processing code exists yet.)
- **A silent crash isn't noticed.** If the whole process (not just one step) crashes
  outright — for example, the machine running it loses power or is forcibly killed
  mid-task — without any error being raised in the normal way, this system has no
  mechanism to notice that on its own. The task would simply appear stuck
  indefinitely; nothing currently watches for and recovers from that scenario.
- **Not every AI-related failure is precisely sorted yet.** The system tries to tell
  "worth retrying" apart from "not worth retrying," but the exact list of error types
  the AI service can produce for things like temporary overload or rate-limiting
  hasn't been fully catalogued in the system's classification logic yet. In practice
  this means some genuinely-temporary AI failures aren't confidently labeled as
  "temporary" — they land in an "unknown" bucket instead (still retried, just not
  precisely categorized).
- **No automatic way to resubmit a document that's been handed off for manual
  review.** Once something lands in the holding area for human attention, there's no
  built-in "retry this one" button or automated re-submission — a person has to
  intervene manually today.
- **No automated tests exist yet specifically for this retry system.** There's no
  repeatable, automated check that verifies the retry/resume/give-up behavior — it
  would need to be exercised through manual testing or a purpose-built test that
  forces failures on demand (see Section 8).

---

## 8. Where to look / how to verify this is working

**For anyone (non-technical):** submit a JD upload and use the processing-status
check for that upload (the system returns a tracking ID at submission time, which can
be used to check progress) to see it move through to completion. If nothing goes
wrong, there's nothing special to see — that's the expected, quiet result. To
observe the retry behavior directly you'd need an actual failure to happen (e.g. a
real, temporary AI-service hiccup at the right moment), which isn't something you can
force through normal use.

**For technical readers:** check these database tables for a given upload's ID:

- `document_processing_stage_executions` — one row per step attempted; should be all
  `SUCCESS` for a clean run.
- `document_processing_checkpoints` — should have **no row** for that ID if
  everything succeeded; a row here means some attempt failed and its progress was
  saved.
- `stage_failure_logs` — should have **no rows** for that ID if everything succeeded;
  each row here is one recorded failed attempt, with what kind of failure it was
  judged to be.
- `dead_letter_queue` — a row here means the document was given up on and handed off
  for manual review; empty is the expected state for anything that's still
  processing normally or already succeeded.
- `celery_task_log` — the overall status of the processing attempt for that upload
  (queued, running, retrying, succeeded, or failed).

An empty result in the checkpoint/failure-log/dead-letter tables for a given upload
means it processed cleanly with no issues. A populated result tells you exactly what
went wrong, when, and whether it was retried or given up on.

---

## 9. Quick reference (technical appendix)

### Stage sequence (`ProcessingStage`, `app/models/async_tasks.py`; order enforced via
`STAGE_ORDER` in `app/services/document_processing/retry_policy.py`)

`VALIDATION → STORAGE → TEXT_EXTRACTION → TEXT_CLEANING → AI_EXTRACTION →
JSON_VALIDATION → SKILL_NORMALIZATION → EMBEDDING_GENERATION → PERSISTENCE`

### Retry limits and backoff (`app/services/document_processing/retry_policy.py`)

| Stage | Max attempts | Base delay | Delay cap |
|---|---|---|---|
| `AI_EXTRACTION` | 5 | 10s | 120s |
| all other stages (`DEFAULT_POLICY`) | 3 | 5s | 60s |

Backoff doubles each attempt: `min(base_delay * 2^(attempt-1), cap)`.

### Failure classification (`app/services/document_processing/error_classifier.py`)

| Exception type | Classification |
|---|---|
| `ConnectionError`, `TimeoutError` | `TRANSIENT` (retried) |
| `ValueError`, `KeyError`, `TypeError` | `PERMANENT` (dead-lettered immediately, regardless of attempt count) |
| anything else | `UNKNOWN` (retried like `TRANSIENT`, up to the stage's `max_attempts`) |

### Data model (`app/models/async_tasks.py`)

**`DocumentProcessingCheckpoint`** (new) — table `document_processing_checkpoints`:
`id` (UUID PK), `task_id` (String(255), unique), `document_type` (enum: `JD`/`RESUME`),
`failed_at_stage` (`ProcessingStage` enum, nullable), `context_data` (JSONB — full
serialized pipeline context), `created_at`, `updated_at`.

**`StageFailureLog`** (new) — table `stage_failure_logs`: `id` (UUID PK), `task_id`
(String(255), not unique — multiple rows accumulate per task), `stage` (enum),
`attempt_number` (SmallInteger), `exception_type` (String(255)), `message` (Text),
`classification` (`FailureClassification` enum: `TRANSIENT`/`PERMANENT`/`UNKNOWN`),
`created_at`.

**`DeadLetterQueue`** (pre-existing table, now actually used) — table
`dead_letter_queue`: `id`, `original_task_id` (FK → `celery_task_log.task_id`),
`task_type` (JD pipeline writes `"JD_DOCUMENT_PROCESSING"`), `resume_id`,
`campaign_candidate_id`, `final_error_message`, `full_error_trace` (always `None`
today — no traceback text is actually captured anywhere), `input_payload` (JSONB —
the checkpoint's saved context, if any), `retry_count`, `first_attempted_at`,
`last_attempted_at`, `moved_to_dlq_at`, `replayed_at`/`replayed_by` (unused, no replay
feature exists), `resolved_at`, `resolution_notes`.

**`CeleryTaskLog`** (pre-existing, one new behavior) — table `celery_task_log`:
`id`, `task_id` (unique), `idempotency_key`, `task_type`, `resume_id`,
`campaign_candidate_id`, `jd_id`, `status` (`QUEUED/RUNNING/PAUSED/SUCCESS/FAILURE/
RETRY/DEAD`), `retry_count`, `worker_hostname`, `input_payload_hash`,
`output_summary`, `token_count`, `error_message`, `queued_at`, `started_at`,
`completed_at`. New: `CeleryTaskLogService.mark_retry()` sets `status=RETRY` and
increments `retry_count`.

**`DocumentProcessingStageExecution`** (pre-existing, unchanged) — table
`document_processing_stage_executions`, unique on (`task_id`, `stage`,
`attempt_number`): `id`, `task_id`, `document_type`, `document_id` (nullable, linked
post-hoc), `stage`, `status` (`PENDING/RUNNING/SUCCESS/FAILED/SKIPPED`),
`attempt_number`, `error_message`, `duration_ms`, `started_at`, `completed_at`,
`created_at`.

**Session note:** checkpoint and failure-log writes happen on a separate database
session (`stage_db`) from business writes (`db`) inside `process_jd_document`, so that
per-stage commits don't prematurely persist an in-progress business record.
`DeadLetterQueueRepository` is the one exception — it writes via the business `db`
session, since `DeadLetterQueue` carries foreign keys tied to business records.

### Key files

| File | Role |
|---|---|
| `app/models/async_tasks.py` | `ProcessingStage`, `FailureClassification`, and all table definitions above |
| `app/repositories/checkpoint_repository.py` | `CheckpointRepository` — get/upsert/delete a checkpoint by `task_id` |
| `app/repositories/stage_failure_log_repository.py` | `StageFailureLogRepository` — append-only failure log records |
| `app/repositories/dead_letter_queue_repository.py` | `DeadLetterQueueRepository` — create dead-letter records |
| `app/services/document_processing/error_classifier.py` | `classify(exc)` → `FailureClassification` |
| `app/services/document_processing/retry_policy.py` | `RetryPolicy`, `STAGE_POLICIES`, `DEFAULT_POLICY`, `STAGE_ORDER`, `compute_backoff_seconds()` |
| `app/services/jd/context_serializer.py` | `to_dict()`/`from_dict()` — serializes/restores the pipeline's in-memory context for checkpoint storage |
| `app/services/document_processing/stage_execution_service.py` | `StageExecutionService` (`start_stage`, `complete_stage`, `run_stage`, `skip_stage`), `StageExecutionError` |
| `app/services/document_processing/retry_driver.py` | `RetryDriver.handle_failure()` — classifies, logs, retries or dead-letters |
| `app/services/jd/jd_processing_pipeline.py` | `JDProcessingPipeline.run()` — loads checkpoint, restores context, skips completed stages, resumes |
| `app/tasks/jd_processing_tasks.py` | `process_jd_document` Celery task — orchestrates the two DB sessions, `RetryDriver`, checkpoint cleanup on success |
| `app/services/celery_task_log_service.py` | `CeleryTaskLogService.mark_retry()` (new), `mark_success`/`mark_failure` (unchanged) |
| `app/api/routes/jd_routes.py` | Generates `task_id` before dispatch; **unchanged** by this mechanism otherwise |
| `app/tasks/campaign_tasks.py` | Confirmed **untouched** — no references to any part of this mechanism |

### Migration note

Two migrations are part of this feature: `c71678d36109` (merge marker, no schema
changes) and `4fd0a3c4f90d` (creates `document_processing_checkpoints` and
`stage_failure_logs`, and the `failure_classification_enum` type). **Note: this
differs from expectations** — running `alembic heads` against this repo currently
fails with an unresolvable migration graph (a duplicate revision id
`265912f5590a`, and several migrations whose `down_revision` points at files that no
longer exist in the repo). This is a pre-existing structural problem in the
migration history, not something introduced by this feature, but it does mean there
is currently no single clean "head" revision to point to.

### Known gaps (see Section 7 for plain-language versions)

- No automated tests exist for this mechanism (`checkpoint_repository`,
  `retry_driver`, `error_classifier`, or the pipeline's resume/skip logic).
- `error_classifier.py` does not yet recognize actual Gemini SDK exception types.
- No dead-letter replay feature consumes retained checkpoints yet.
- No watchdog detects a task stuck in `RUNNING` from a silent worker crash.
- `StageExecutionService.next_attempt_number()` exists but is not called anywhere —
  attempt numbers come from Celery's own retry counter instead.
