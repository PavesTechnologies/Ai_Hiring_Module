# M10 - Candidate Ranking

## Overview

Candidate Ranking combines the three independent scoring layers already
produced elsewhere in the pipeline - Deterministic (mandatory-skill/
experience/education matching), Semantic (resume-to-JD embedding
similarity), and AI Evaluation (LLM-based assessment, M09) - into a single
**Composite Score** per candidate, weighted by the owning campaign's
configured scoring weights. The composite score is the ranking signal HR
uses to sort/shortlist candidates within a campaign; it is derived data,
never a source of truth, and can always be recomputed from its inputs.

This document covers **Epic 1: Composite Score Calculation**.

---

## Epic 1: Composite Score Calculation

### What was implemented

- **Composite Score Engine** - `CompositeScoringService`
  (`app/services/campaign/composite_scoring_service.py`) is the **single
  source of truth** for `campaign_candidates.composite_score` - no other
  service, repository, API, helper or Celery task writes that column or
  inserts into `candidate_composite_score_history`.
- **Celery Task** - `scoring.calculate_composite_score`
  (`app/tasks/composite_scoring_tasks.py`), reusing the exact same
  RetryPolicy/DeadLetterQueue/task-log/idempotency machinery already
  established by `calculate_semantic_score_task`.
- **Validation** - `CompositeScoringService` validates candidate/campaign
  existence, that campaign weights sum to exactly 100.00, and that each
  present score component is within its valid range - defensively, never
  relying on upstream validation alone.
- **No Weight Redistribution** - campaign weights are always used exactly
  as configured. A missing score component is COALESCEd to 0, never
  excluded, and never causes any other component's weight to change.
- **Rounding Strategy** - every intermediate value stays full-precision
  `Decimal`; only the final composite score is rounded, via one reusable
  helper (`round_composite_score`).
- **Audit** - one `COMPOSITE_SCORE_COMPUTED` audit log entry per
  calculation.
- **History** - one immutable `candidate_composite_score_history` row per
  calculation.
- **Automatic Recalculation** - triggered by exactly two events: AI
  evaluation completing (placeholder - M09 not yet built), and a campaign
  weight change. Never by resume upload/parsing/reprocessing/reset, a
  deterministic/semantic completion, or an HR override.
- **Campaign Weight Handling** - a weight change recalculates ONLY
  composite_score for every existing candidate in that campaign;
  deterministic/semantic/AI scores are never recomputed.
- **Modular Service Structure** - `CompositeScoringService` is split into
  one method per responsibility (`validate_inputs`, `normalize_scores`,
  `calculate_score`, `round_score`, `persist`, `create_history`,
  `write_audit`), orchestrated by `calculate_and_store_composite_score`,
  rather than one large method.

---

### Database Changes

**New table: `candidate_composite_score_history`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `campaign_candidate_id` | UUID, FK -> `campaign_candidates.id` | |
| `deterministic_score` | NUMERIC(5,2), nullable | raw, as stored on the candidate at calculation time |
| `semantic_score` | NUMERIC(7,6), nullable | raw 0-1 cosine similarity |
| `normalized_semantic_score` | NUMERIC(7,4), nullable | `semantic_score * 100`, the scale used in the formula |
| `effective_ai_score` | NUMERIC(5,2), nullable | |
| `weight_deterministic` / `weight_semantic` / `weight_ai` | NUMERIC(5,2) | the campaign's configured weights at calculation time, used exactly as-is |
| `composite_score` | NUMERIC(6,3) | the rounded result |
| `formula_version` | VARCHAR(20) | `COMPOSITE_SCORE_FORMULA_VERSION` |
| `trigger_source` | enum (`AI_EVALUATION`, `CAMPAIGN_WEIGHT_CHANGE`) | |
| `calculated_at` | TIMESTAMPTZ | |

Indexes: `campaign_candidate_id`, `calculated_at`. Append-only - rows are
never updated or deleted.

> There are no `normalized_weight_*` columns - weights are never
> redistributed, so the configured `weight_*` columns are always the
> weights actually used.

**New column:** `campaign_candidates.composite_score_computed_at` (nullable
timestamp) - when `composite_score` was last (re)computed.
`campaign_candidates.composite_score` itself already existed and is
reused as-is.

**Migration:** `alembic/versions/b1f4c9a2e7d3_composite_scoring_support.py`,
branching off `e2c8a4f6b9d1` (the most recent scoring-related Alembic
head). Also adds `COMPOSITE_SCORE_COMPUTED` to the `audit_action_type_enum`.

> **Note:** this repository currently has multiple concurrent Alembic
> heads (pre-existing, unrelated to this epic - verified via
> `alembic heads`). This migration deliberately branches off the most
> scoring-relevant head rather than attempting to merge every unrelated
> head, which is outside this epic's scope.

---

### Service Layer

**New:** `CompositeScoringService`
(`app/services/campaign/composite_scoring_service.py`) - the single
source of truth for `composite_score`. Public entry point
`calculate_and_store_composite_score(campaign_candidate_id, trigger_source)`
orchestrates:

1. `validate_inputs()` - fetches candidate + campaign, validates weights
   sum to 100.00, validates each present score is in range.
2. `normalize_scores()` - COALESCEs missing scores to 0, rescales
   `semantic_score` to a 0-100 scale.
3. `calculate_score()` - weighted sum using the campaign's configured
   weights, unmodified.
4. `round_score()` - the only rounding step (2 decimal places).
5. `persist()` - writes `composite_score` + `composite_score_computed_at`.
6. `create_history()` - inserts the immutable history row.
7. `write_audit()` - writes the `COMPOSITE_SCORE_COMPUTED` audit entry.

Returns the full breakdown dict; commit is left to the caller.

**Modified:**
- `CampaignCandidateService` (`app/services/campaign/campaign_candidate_service.py`)
  - `_queue_post_override_evaluation` re-queues AI evaluation and semantic
    scoring on an HR override (restarting the remaining scoring pipeline)
    and does **not** enqueue composite scoring - composite score is
    recalculated once AI evaluation eventually completes, not by the
    override itself.
- `CampaignService` (`app/services/campaign/campaign_service.py`)
  - Helper `_enqueue_composite_recalculation_for_campaign(campaign_id)`.
  - `update_scoring_configuration` and `update_campaign` both call it,
    best-effort, right after their existing commit, but only when the
    diff actually touched `weight_deterministic`/`weight_semantic`/
    `weight_ai` (a thresholds-only change does not trigger it).

---

### Repository Changes

- `CampaignCandidateRepository` (`app/repositories/campaign_candidate_repository.py`)
  - Added `get_ids_by_campaign(campaign_id)` - bare candidate ids for a
    campaign, used only to fan out weight-change recalculation.
  - `reset_for_resubmission` now also clears `composite_score_computed_at`.
- **New:** `CandidateCompositeScoreHistoryRepository`
  (`app/repositories/candidate_composite_score_history_repository.py`) -
  `create` (append-only insert) and `get_by_campaign_candidate_id`.
- No other repository was modified. `CampaignRepository.get_by_id` and
  `CampaignCandidateRepository.get_by_id`/`update` are reused as-is.

---

### Celery Flow

```
AI Evaluation completes (M09, future placeholder) ──┐
                                                      ├──▶ scoring.calculate_composite_score ──▶ campaign_candidates.composite_score
Campaign Weight Changed                             ─┘         │                                (+ composite_score_computed_at)
                                                                 ├──▶ candidate_composite_score_history (INSERT, immutable)
                                                                 └──▶ audit_log (COMPOSITE_SCORE_COMPUTED)

HR Override ──▶ restarts remaining pipeline (AI_EVALUATE + semantic scoring) ──▶ (eventually) AI Evaluation completes ──▶ above
```

Both trigger sites call the same shared, idempotent enqueue helper,
`_enqueue_composite_scoring` (`app/tasks/composite_scoring_tasks.py`) - a
QUEUED/RUNNING `celery_task_log` row for the same `campaign_candidate_id` +
`COMPOSITE_SCORE` already means a calculation is in flight, so nothing
enqueues a second one. `campaign_candidate_service.py` no longer imports
this helper at all - an HR override has no composite-scoring call site.

---

### Composite Formula

```
composite_score = ROUND_2(
    (weight_deterministic / 100) * COALESCE(deterministic_score, 0)
  + (weight_semantic      / 100) * COALESCE(semantic_score, 0) * 100
  + (weight_ai            / 100) * COALESCE(effective_ai_score, 0)
)
```

**Semantic normalization:** `semantic_score` is stored as a 0-1 cosine
similarity (`Numeric(7,6)`); it is multiplied by 100 before being combined
with `deterministic_score`/`effective_ai_score` (both already 0-100). Both
the raw and normalized values are persisted to
`candidate_composite_score_history`.

**No weight redistribution:** campaign weights (`weight_deterministic`,
`weight_semantic`, `weight_ai`) are always used exactly as configured on
the campaign. A missing score component is COALESCEd to 0 - its weight is
**not** reassigned to the other components. Example: weights 30/40/30 with
`effective_ai_score` missing -> AI contributes `30/100 * 0 = 0`; the
deterministic and semantic weights remain 30 and 40, unchanged. If all
three components are missing, the result is simply `0.00` - not an error.

**Rounding:** every intermediate value (the running sum) stays a
full-precision `Decimal`. Only the final result is rounded, to 2 decimal
places, via `round_composite_score` (`app/utils/scoring_utils.py`) - the
single reusable helper; no other code path rounds a composite score.

**Formula version:** `COMPOSITE_SCORE_FORMULA_VERSION` in
`app/enums/constants.py`, currently `"v1"`. Every calculation (candidate
record and history row alike) stamps this same constant - never hardcoded
a second time anywhere else.

---

### Trigger Sources

Composite Score has **exactly two** valid triggers:

| Trigger | Where | `trigger_source` |
|---|---|---|
| AI Evaluation completes | **Placeholder** - M09 does not exist yet in this codebase. `app/tasks/composite_scoring_tasks.py` documents the exact integration point: once built, M09's task must call `_enqueue_composite_scoring(..., trigger_source=CompositeScoreTriggerSource.AI_EVALUATION)` right after its own commit, the same "best-effort, post-commit" convention `calculate_deterministic_score_task` already uses to auto-trigger semantic scoring. | `AI_EVALUATION` |
| Campaign weight change | `CampaignService._enqueue_composite_recalculation_for_campaign`, called from `update_scoring_configuration` and `update_campaign` | `CAMPAIGN_WEIGHT_CHANGE` |

Explicitly **not** a trigger: resume upload, resume parsing, resume
reprocessing, resume reset, deterministic completion, semantic completion,
**and HR override**.

**HR Override is not a Composite Score trigger.** When HR overrides a
scoring metric, the correct flow is:

```
HR overrides a scoring metric
        ↓
Continue the remaining scoring pipeline (AI_EVALUATE + semantic scoring re-queued)
        ↓
AI Evaluation (eventually completes)
        ↓
Composite Score (triggered by AI Evaluation completing, not by the override)
```

`CampaignCandidateService._queue_post_override_evaluation` only re-queues
the remaining pipeline steps; it has no composite-scoring call site.

---

### Validation Rules

`CompositeScoringService.validate_inputs()` never relies on upstream
validation alone:

1. **Candidate existence** - `ValueError` (permanent, no retry) if missing.
2. **Campaign existence** - `ValueError` (permanent, no retry) if missing.
3. **Weight validation** - `weight_deterministic + weight_semantic +
   weight_ai == 100.00`, re-checked immediately before every computation
   (defensive - the DB's own `chk_weights_sum_100` CHECK constraint should
   make this unreachable). On failure: abort before reading any candidate
   score, log an error, raise `InvalidScoringWeightsError` (a `ValueError`
   subclass -> classified `PERMANENT` -> dead-lettered immediately, never
   retried, and the whole transaction rolls back with no partial writes).
4. **Score range validation** - `deterministic_score`/`effective_ai_score`
   must be within `[0, 100]`; `semantic_score` must be within `[0, 1]`.
   `None` (not yet scored) is always valid. On failure: raise
   `InvalidScoreRangeError` (also a `ValueError` subclass - same permanent,
   no-retry, full-rollback handling).
5. **Campaign status** - a campaign not `ACTIVE`/`PAUSED` is skipped
   gracefully by the Celery task (not a failure), same gate every other
   scoring task uses - checked before `CompositeScoringService` is even
   called.
6. **Missing score components** - NOT a failure. COALESCEd to 0 by
   `normalize_scores()`; the calculation always proceeds.

---

### Audit Trail

- **Audit Log** - one `ActionType.COMPOSITE_SCORE_COMPUTED` entry per
  calculation, on `EntityType.CAMPAIGN_CANDIDATE`, with the full breakdown
  (raw + normalized scores, configured weights, composite score, formula
  version, trigger source) as `details`. Written only on success -
  validation failures are logged via `logger.error` and dead-lettered
  (see Error Handling), not written as a separate audit entry.
- **History Table** - `candidate_composite_score_history` is the
  authoritative, immutable, query-friendly trail (never updated or
  deleted) - one row per calculation, indexed by
  `campaign_candidate_id` and `calculated_at`. Stores raw semantic score,
  normalized semantic score, composite score, formula version, trigger
  source, configured weights, and timestamp.
- **Formula Version** - stamped on every history row and audit entry, from
  the single `COMPOSITE_SCORE_FORMULA_VERSION` constant.
- **Trigger Source** - stamped on every history row and audit entry, one
  of the two `CompositeScoreTriggerSource` enum values.

---

### API Changes

None required. `composite_score` is already exposed on every existing
candidate-facing response
(`CandidateScorecardResponse`/`CampaignCandidateResponse`/
`CandidateCampaignHistoryEntryResponse`, etc.) and continues to read
directly off `campaign_candidates.composite_score` - no new endpoint or
response field was added for this epic.

---

### Error Handling

- **Rollback** - the Celery task rolls back its entire DB session on any
  exception before deciding whether to retry or dead-letter; a validation
  failure inside `CompositeScoringService` never leaves a partial write
  (nothing is persisted, no history row inserted, no audit entry written).
- **Retry** - transient failures (anything not classified `PERMANENT` by
  `error_classifier.classify`) are retried up to 3 attempts with
  exponential backoff (`RetryPolicy(max_attempts=3, base_delay_seconds=10,
  max_delay_seconds=120)`), the same policy shape as semantic scoring.
- **Dead Letter** - retries exhausted (or an immediately-permanent failure
  like `InvalidScoringWeightsError`/`InvalidScoreRangeError`) writes one
  `dead_letter_queue` row and marks the task log `DEAD`. Never re-raised
  past that point.
- **Validation failures** - `InvalidScoringWeightsError` and
  `InvalidScoreRangeError` are both `ValueError` subclasses, so they are
  classified `PERMANENT` and dead-lettered on the first attempt, never
  retried.
- **Concurrency** - no locking is used. If multiple triggers fire for the
  same candidate at nearly the same time, each reads the latest committed
  values, computes independently, and writes; the last commit wins. This
  is acceptable because `composite_score` is entirely derived data and can
  always be recomputed.

---

### Testing

- **Unit tests** - `tests/services/campaign/test_composite_scoring_service.py`:
  COALESCE-to-zero for each missing component (and all three at once,
  which yields `0.00`, not an error), configured weights always used
  unchanged (no redistribution), invalid weights, out-of-range scores for
  each of the three components, rounding to 2 decimals, semantic
  normalization, history-row content (raw + normalized semantic score,
  configured weights), audit logging, and each modular method
  (`normalize_scores`/`calculate_score`/`round_score`) independently.
- **Task tests** - `tests/tasks/test_composite_scoring_tasks.py`: graceful
  skips (candidate/campaign missing, campaign not scoreable), computing
  even when only one score component is present, immediate dead-letter on
  invalid weights, retry on transient failure, commit + audit on success,
  idempotent duplicate-run skip, and the shared `_enqueue_composite_scoring`
  helper (idempotency, dispatch, and swallowed enqueue failures) exercised
  only via the two valid triggers.
- **Trigger-wiring tests**:
  - `tests/services/campaign/test_campaign_candidate_override_service.py`
    - an HR override enqueues AI_EVALUATE + semantic scoring, and
      explicitly does **not** import or call `_enqueue_composite_scoring`.
  - `tests/services/campaign/test_campaign_service_composite_recalculation.py`
    - a campaign weight change fans out recalculation to every candidate
      in the campaign, is best-effort, and does NOT fire on a
      thresholds-only change.
- **Edge cases covered**: each score component missing individually and
  all three at once, invalid weights, out-of-range scores, HR override
  correctly NOT triggering composite scoring, campaign weight-change
  trigger (vs. threshold-only changes), rounding, history insertion,
  audit logging. Concurrent execution is handled by design (no locking,
  last-commit-wins) rather than tested with real concurrency.

---

### Future Integration

**M09 AI Evaluation** does not exist yet in this codebase. The integration
point is a documented placeholder/TODO in
`app/tasks/composite_scoring_tasks.py`: once M09's Celery task is built,
it must call
`_enqueue_composite_scoring(campaign_candidate_id, task_log_service,
CompositeScoreTriggerSource.AI_EVALUATION)` immediately after its own
transaction commits successfully - the same best-effort, post-commit
convention `calculate_deterministic_score_task` already uses to
auto-trigger semantic scoring. No change to this epic's code should be
required when that happens; only a new call site inside the AI evaluation
task itself. Composite Score is then automatically (re)triggered every
time AI evaluation completes for a candidate - the same event an HR
override's restarted pipeline eventually reaches too - in addition to the
campaign weight-change trigger already wired up today.

---

## Epic 2: Campaign Weight Configuration History & Recalculation

### Overview

Epic 1 made Composite Score itself auditable (one immutable
`candidate_composite_score_history` row per calculation, plus an audit
log entry). Epic 2 extends that same auditability guarantee one level up
the chain: to the campaign's scoring **weights** themselves. Every time
`weight_deterministic`/`weight_semantic`/`weight_ai` actually change on a
campaign, that change becomes a permanent, immutable historical record -
independent of, and in addition to, the pre-existing weight-change diff
already captured inline inside `CAMPAIGN_SCORING_CONFIG_CHANGED` audit
entries.

### Business Purpose

Before Epic 2, a campaign's weight-change history existed only as
free-form `changes` dicts embedded inside generic `CAMPAIGN_SCORING_CONFIG_CHANGED`/
`CAMPAIGN_UPDATED` audit log entries - correct, but not queryable as
structured before/after weight data, and not distinguished from a
thresholds-only change. Epic 2 gives HR_ADMIN a dedicated, structured,
permanent trail of exactly when and by whom a campaign's *scoring
weights* (as opposed to thresholds) changed, tied to the exact
composite-score formula version in effect at that moment - the same
tightening Epic 1 already applied to composite-score calculations
themselves.

### Architecture

No new services, repositories, Celery tasks, or APIs were introduced
beyond the one repository this epic's own new table requires
(`CampaignWeightConfigurationHistoryRepository` - justified below).
Every other requirement is delivered by **extending** the existing
`CampaignService` (`update_scoring_configuration` and `update_campaign`,
the two pre-existing scoring-edit paths) and by **reusing** Epic 1's
existing Composite Score recalculation machinery (`CompositeScoringService`,
`scoring.calculate_composite_score`, `_enqueue_composite_scoring`,
`_enqueue_composite_recalculation_for_campaign`) completely unmodified.

Why a new repository was necessary: `campaign_weight_configuration_history`
is a brand-new, append-only table with no existing repository owning it -
`CampaignRepository` owns the mutable `hiring_campaigns` row, not its
immutable change history. This mirrors the exact precedent Epic 1 already
established for `CandidateCompositeScoreHistoryRepository`. No new
`CampaignWeightService`/`CampaignWeightHistoryService`/`WeightConfigurationService`
was created - `CampaignService` already owns both scoring-edit paths, and
splitting weight-history logic into a separate service would duplicate
the validation, change-detection, and transaction-boundary logic those
two methods already have, rather than reusing it.

### Database Changes

**New table: `campaign_weight_configuration_history`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `campaign_id` | UUID, FK -> `hiring_campaigns.id` | |
| `old_weight_deterministic` / `old_weight_semantic` / `old_weight_ai` | NUMERIC(5,2) | the campaign's weights immediately before this change |
| `new_weight_deterministic` / `new_weight_semantic` / `new_weight_ai` | NUMERIC(5,2) | the campaign's weights immediately after this change |
| `changed_by` | VARCHAR(255), FK -> `users.id`, nullable | |
| `changed_at` | TIMESTAMPTZ | |
| `formula_version` | VARCHAR(20) | reuses `COMPOSITE_SCORE_FORMULA_VERSION` - never a second/independent version constant |

Indexes: `campaign_id`, `changed_at`. Append-only - rows are never
updated or deleted, matching `candidate_composite_score_history`'s own
convention exactly.

**Migration:** `alembic/versions/c2e6a1f8d4b7_campaign_weight_configuration_history.py`,
branching off `b1f4c9a2e7d3` (Epic 1's own migration). Also adds
`CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED` to the `audit_action_type_enum`.

**Schema/validation gap closed:** `CampaignUpdateRequest`'s
`weight_deterministic`/`weight_semantic`/`weight_ai` fields previously had
no `ge=0, le=100` bounds (unlike the sibling `CampaignScoringUpdateRequest`,
which already had them) - meaning a PATCH could submit e.g.
`weight_deterministic=-50, weight_semantic=200, weight_ai=-50` (which sums
to 100.00) and it would pass every check that existed at the time.
`_validate_scoring_weights` (shared by both scoring-edit paths) now also
rejects any individual weight outside `[0, 100]`, and the schema itself
was updated to match its sibling. Both changes are strictly tightening -
every request that was valid before remains valid.

### History Flow

```
Validate (candidate/campaign exists, weights in [0,100], weights sum to 100.00)
        ↓
Persist campaign (existing CampaignRepository.update_scoring_configuration / .update)
        ↓
Persist history snapshot (CampaignWeightConfigurationHistoryRepository.create)  <- only if weight fields actually changed
        ↓
Write Audit (CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED)                              <- only if weight fields actually changed
        ↓
Commit (single commit for campaign + history + every audit entry written above)
        ↓
Queue Composite Recalculation (best-effort, post-commit, via existing _enqueue_composite_recalculation_for_campaign)  <- only if weight fields actually changed
```

This is the exact same transaction shape both `update_scoring_configuration`
and `update_campaign` already used for Epic 1's recalculation trigger -
Epic 2 inserts the history/audit step into that same pre-existing
sequence, gated by the same `weight_fields_changed` condition that already
gated recalculation, rather than introducing a parallel code path.

### Validation

`CampaignService._validate_scoring_weights` (shared by both scoring-edit
paths, unchanged in name/signature/callers) now performs, in order:
1. Each individual weight is within `[0, 100]` (Epic 2 addition).
2. `weight_deterministic + weight_semantic + weight_ai == 100.00` (pre-existing).
3. No weight falls below the configured `MIN_LAYER_WEIGHT` (pre-existing).

Campaign existence is checked before any of the above (pre-existing,
`get_by_id_for_update` returning `None` -> 404). Any failure aborts before
any write - the campaign row, the history row, and any audit entry are
all still unpersisted (flushed at most, never committed) - so the caller's
existing `except Exception: self.campaign_repo.rollback(); raise` discards
everything as one atomic unit. `update_scoring_configuration` did not
previously have this try/except wrapper; it was added as part of Epic 2
specifically to guarantee "no partial writes" (`update_campaign` already
had it).

### Audit

New `ActionType.CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED` (`app/enums/constants.py`),
written by the new shared helper `CampaignService._record_weight_configuration_change`
whenever - and only whenever - a weight field actually changes. The audit
`details` payload includes: campaign name/id, `old_weights` (all three,
as strings), `new_weights` (all three, as strings), `changed_by`, and
`formula_version`. This is written **in addition to**, not instead of, the
pre-existing `CAMPAIGN_SCORING_CONFIG_CHANGED`/`CAMPAIGN_UPDATED` audit
entry each method already wrote (which still fires for threshold-only
changes too) - Epic 2 never removed or altered that pre-existing
behavior.

### Celery Flow

Unchanged from Epic 1 - Epic 2 introduces no new Celery task and no new
enqueue helper:

```
Campaign Weight Changed (update_scoring_configuration / update_campaign)
        ↓ (after commit, best-effort)
CampaignService._enqueue_composite_recalculation_for_campaign
        ↓
CampaignCandidateRepository.get_ids_by_campaign (bulk fetch of ids only)
        ↓ (one per candidate)
composite_scoring_tasks._enqueue_composite_scoring (existing idempotency guard)
        ↓
scoring.calculate_composite_score (existing task, existing RetryPolicy/DLQ)
        ↓
CompositeScoringService.calculate_and_store_composite_score
```

Per the epic's explicit constraint, a campaign weight change **never**
recalculates deterministic_score, semantic_score, or effective_ai_score -
only composite_score, exactly as Epic 1 already established (Design
Decision 9) and exactly as `CompositeScoringService` already enforces by
construction (it only ever reads those three inputs, never recomputes
them).

### Transaction Flow

Both scoring-edit paths now share this exact structure:

```python
try:
    campaign = self.campaign_repo.get_by_id_for_update(campaign_id)   # locking read
    ...validate...
    old_weights = {...} if weight_fields_changed else None            # captured BEFORE mutation
    ...mutate/persist campaign...
    ...existing audit (thresholds/updated)...
    if weight_fields_changed:
        self._record_weight_configuration_change(campaign, old_weights, updated_by)
    self.campaign_repo.commit()
    if weight_fields_changed:
        self._enqueue_composite_recalculation_for_campaign(campaign.id)
    ...build and return response...
except Exception:
    self.campaign_repo.rollback()
    raise
```

`get_by_id_for_update` (a pre-existing `SELECT ... FOR UPDATE` locking
read, already used by candidate-creation's cap-check race guard) replaces
the plain `get_by_id` both methods previously used for their own initial
fetch - see Concurrency Handling below.

### Edge Cases

| Edge case | Handling |
|---|---|
| Campaign does not exist | `get_by_id_for_update` returns `None` -> `CampaignException(404)`, rolled back |
| Invalid weights / sum != 100 / negative / >100 | `_validate_scoring_weights` rejects before any write; rolled back |
| Campaign with zero candidates | `get_ids_by_campaign` returns `[]`; fan-out loop is a no-op; history/audit for the weight change itself still recorded |
| Campaign with thousands of candidates | Unchanged from Epic 1 - same bulk id fetch + per-candidate idempotent enqueue, reused as-is |
| Duplicate updates / repeated same weight values | No-Op Detection (below) |
| Concurrent updates | `get_by_id_for_update` serializes two concurrent weight-change requests against the same campaign row |
| Recalculation already running | Pre-existing per-candidate idempotency guard in `_enqueue_composite_scoring` (QUEUED/RUNNING `celery_task_log` row skips re-enqueue) |
| Celery failure / retry | Unchanged from Epic 1 - existing `RetryPolicy`/`error_classifier`/`DeadLetterQueueRepository` |
| Celery enqueue failure after commit | `_enqueue_composite_recalculation_for_campaign` already wraps its own body in try/except that only logs - the already-committed campaign/history/audit are never undone |
| Audit failure / history persistence failure | Both happen before `commit()`, inside the same try block - either failing rolls back the campaign update too; no partial writes |
| Optimistic concurrency | Not implemented as a versioned-column scheme (would require a `version_id_col` on `HiringCampaign`, affecting every other update path to this model) - handled instead via the pessimistic `get_by_id_for_update` lock, consistent with this codebase's existing precedent |
| Retry safety / idempotency of recalculation itself | Unchanged from Epic 1 - per-candidate `celery_task_log` idempotency guard |
| No partial updates | Guaranteed by the single-transaction structure above |

### No-op Detection

Both `update_scoring_configuration` and `update_campaign` already compute
a `changes`/`scoring_changes` diff dict by comparing each requested field
against the campaign's current value (`Decimal(...) != Decimal(...)`) -
this is Epic 1's pre-existing weight-change-detection logic, reused
as-is. Epic 2 gates history/audit/recalculation on
`weight_fields_changed = _WEIGHT_FIELDS & changes.keys()` (or
`scoring_changes.keys()`), the exact same set already used to gate
Epic 1's recalculation trigger. Consequently: **if the recruiter submits
exactly the current weights, `weight_fields_changed` is empty by
construction, and history/audit/recalculation are all skipped
automatically** - no separate no-op-detection code was written, because
none was needed. The method still returns success (the pre-existing
"update" call and commit still run - a thresholds-only or genuinely
identical resubmission is not itself an error).

### Concurrency Handling

Both scoring-edit paths now use `CampaignRepository.get_by_id_for_update`
(a pre-existing `SELECT ... FOR UPDATE` locking read) instead of the
plain `get_by_id` they used before. Two concurrent weight-change requests
against the same campaign now serialize: the second transaction blocks
until the first commits or rolls back, then reads the first's already-
committed result. This closes the race that previously existed (both
requests reading stale data and racing to commit last-write-wins) without
introducing a new locking primitive - `get_by_id_for_update` was already
used elsewhere in this codebase (candidate-creation's cap-check guard) for
exactly this kind of campaign-row race.

### Performance Considerations

No new N+1 patterns introduced. `_record_weight_configuration_change`
performs exactly one INSERT (history) and one audit-log write per actual
weight change - O(1) regardless of campaign size, since it operates on
the campaign row, not per-candidate. The subsequent Composite Score
recalculation fan-out is entirely unchanged from Epic 1: one bulk
`get_ids_by_campaign` query (ids only, no joined Candidate/Resume rows)
followed by a per-candidate idempotency check + enqueue - the same
batching this epic was explicitly told to reuse rather than re-optimize.

### Rollback Behaviour

Every write this epic introduces (`campaign_weight_configuration_history`
row, `CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED` audit entry) happens strictly
before the method's single `commit()` call, inside the same
`try/except Exception: rollback(); raise` block that already wraps the
rest of the method. A failure at any point - validation, the campaign
UPDATE itself, the history INSERT, or the audit-log write - rolls back
everything written so far in that request; Celery is never reached in
that case (`_enqueue_composite_recalculation_for_campaign` is only called
after `commit()` returns successfully).

### API Changes

None. No new endpoint was added or needed - `PUT/PATCH` requests to the
existing scoring-configuration and campaign-update endpoints now also
produce a history row and a dedicated audit entry as a side effect, with
no change to their request or response shapes.

### Repository Changes

- **New:** `CampaignWeightConfigurationHistoryRepository`
  (`app/repositories/campaign_weight_configuration_history_repository.py`) -
  `create` (append-only insert), `get_by_campaign_id` (read, for any
  future history-viewing use), `commit`/`rollback`. No `update`/`delete` -
  history rows are immutable by construction (there is no code path that
  could mutate one).
- `CampaignRepository` - **unchanged**. `get_by_id_for_update` already
  existed and is reused as-is.
- `CampaignCandidateRepository` - **unchanged**. `get_ids_by_campaign`
  (Epic 1) is reused as-is for recalculation fan-out.

### Service Changes

`CampaignService` (`app/services/campaign/campaign_service.py`), extended
in place - no new service class:
- Constructor: new optional `campaign_weight_configuration_history_repo`
  parameter, defaulted from `db` (same convention as
  `circuit_breaker_repo`/`dead_letter_queue_repo`) - no change required to
  `app/dependencies/campaign.py`'s existing DI wiring.
- `_validate_scoring_weights` - extended with the `[0, 100]` per-field
  range check.
- **New** private helper `_record_weight_configuration_change` - shared by
  both scoring-edit paths.
- `update_scoring_configuration` - now wrapped in `try/except` (previously
  unwrapped), uses `get_by_id_for_update`, captures `old_weights` before
  mutation, calls the new helper when weights actually changed.
- `update_campaign` - uses `get_by_id_for_update`, captures `old_weights`
  before its own mutation loop, calls the new helper when weights actually
  changed. Also fixes a pre-existing, unrelated bug: the method's response
  referenced an `updated_prompt` local variable that was never assigned
  anywhere, so any successful `update_campaign` call raised `NameError`
  before returning a response. Fixed by initializing `updated_prompt =
  None` and setting it inside the existing prompt-reassignment branch -
  the same "default None, set only inside its own conditional" pattern
  already used for `previous_hiring_manager_id` a few lines below it.

### Testing

`tests/services/campaign/test_campaign_weight_configuration_history.py`
(new): weight range validation (negative, >100, valid), the
`_record_weight_configuration_change` helper (history-row content, audit
content), `update_scoring_configuration` (no-op skip, weight-change
records history/audit/recalculation, thresholds-only change skips
weight-specific history, invalid-weight rollback, not-found rollback,
locking read), `update_campaign` (weight-change records history/
recalculation, no-op skip, locking read, invalid-weight rollback), and
the history repository's immutability contract (no `update`/`delete`
method exists at all).

`tests/services/campaign/test_campaign_service_composite_recalculation.py`
(Epic 1, unmodified) continues to pass unchanged, confirming the
recalculation fan-out itself was not altered by this epic.

### Future Scope

Explicitly out of scope for this epic (per its own instructions), left
for a future epic if needed: Ranking Preview / temporary simulation
(compute-without-persisting), weight redistribution, deterministic
sub-weight configuration (per-campaign skill/experience/education
blend), new ranking APIs, and new candidate APIs. A dedicated endpoint
to read `campaign_weight_configuration_history` directly (as opposed to
via the existing `GET /{campaign_id}/scoring-history`, which reads from
the audit log) was not requested and was not added - `get_by_campaign_id`
exists on the new repository for exactly that purpose if/when it is.
