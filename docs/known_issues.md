# Known Issues

Tracked structural/process issues that are known, understood, and deliberately
not fixed in the change that discovered or re-encountered them. Each entry
should stay short: what's wrong, where it's been seen, and what the standing
workaround is until someone actually fixes it.

---

## M12: 2 pre-Epic-4 candidates at INTERVIEW with no `interview_schedules` row (backfilled 2026-08-17)

**Status:** resolved. One-time backfill, not evidence of an ongoing gap.

Found during the post-merge interview-endpoint contract re-verification
(confirming the `campaign_service.py` caching-layer merge hadn't
disturbed the 3 interview scheduling endpoints): 2 `campaign_candidates`
rows at `pipeline_stage = 'INTERVIEW'`
(`f66475ec-17ed-41e7-b2b4-965654ca7151`,
`641044a6-3caf-4cb3-9b3d-22d47125b1d2`, both in campaign
`9765547c-4885-4138-8002-87c8948ec512`) had no matching
`interview_schedules` row at all.

**Root cause:** both candidates transitioned to `INTERVIEW` before Epic 4
Step 2's `StageTransitionService.transition()` hook existed - the hook
that auto-creates a `PENDING` `interview_schedules` row on every
HM_REVIEW/FRAUD_REVIEW -> INTERVIEW transition. Their transitions predate
that code; nothing about the hook itself has ever failed to fire for a
transition that ran after it was deployed. Practical impact: opening
either candidate's schedule form would hit an unhandled 409 (`schedule()`
only succeeds from an existing `PENDING` row - there's no row at all for
these two to be in the wrong state, they're just missing one).

**How it was fixed:** a one-time script (not a permanent code path) ran
`InterviewScheduleRepository.get_or_create_pending(campaign_candidate_id)`
directly against the live DB for both ids - the exact same method (same
shape, same defaults) `transition()`'s hook itself calls, so the backfilled
rows are indistinguishable from ones the hook would have created at the
time. Both inserted as fresh `PENDING` rows (`was_created=True`, every
other column null). Confirmed afterward: zero `campaign_candidates` at
`INTERVIEW` without a matching `interview_schedules` row (3 candidates at
`INTERVIEW`, 3 `interview_schedules` rows), and zero duplicate
`campaign_candidate_id` rows (the `UNIQUE(campaign_candidate_id)`
constraint was never at risk, but checked directly rather than assumed).

**Correction (2026-08-17, later the same day):** the closing claim below
was wrong, and it's worth naming plainly why rather than quietly editing
it away. It said:

> the hook covers 100% of transitions from the moment it was deployed
> onward - this backfill exists only to cover the handful of candidates
> that moved before that moment.

That was true only for transitions going through
`StageTransitionService.transition()` - it implicitly assumed that was
the *only* way `pipeline_stage` could ever become `INTERVIEW`. It isn't.
A third candidate hit the identical unhandled-409 symptom the same day,
via a completely different code path that was never touched by the Epic
4 Step 2 hook at all. See the next entry for the full finding and fix -
this entry's backfill and root-cause description for the original 2
candidates are still accurate, only the "not an ongoing gap" conclusion
was overclaimed.

---

## M12: the INTERVIEW-entry hook was missing from 2 of 3 real pipeline-stage-writing paths (fixed 2026-08-17)

**Status:** resolved. Confirmed via full codebase sweep - all 3 real
`pipeline_stage` writers now carry the hook; no others exist.

Surfaced within hours of the entry above, via a live 409 during frontend
integration testing on a candidate that had genuinely reached
`INTERVIEW` (`6706546a-0dcf-48f1-9567-62163122b697`, campaign
`8ad83be2-7222-4f2c-89e4-7b66d2fcfb62`) - the entry above's "not an
ongoing gap" claim turned out to be wrong within the same day it was
written, which is exactly why it's being corrected in place above rather
than left to stand.

**Root cause:** this codebase has **three** independent code paths that
can write `campaign_candidates.pipeline_stage`, not one:

1. `StageTransitionService.transition()` - HM_REVIEW/FRAUD_REVIEW ->
   INTERVIEW, used by Epic 1's `advance_to_interview`. **Had** the Epic 4
   Step 2 hook already.
2. `PipelineTransitionService.transition_stage()` - the generic engine
   behind `move_pipeline_stage` (Pipeline Board drag-and-drop) and
   `BulkStageMoveService`. Its own class docstring claimed "nothing in
   the codebase calls this yet... still zero call sites of its own" -
   true when Epic 3 wrote it, false by the time Epic 4 shipped (both
   real callers above already existed), and nobody caught the staleness
   because **this class had zero test coverage anywhere** - the one test
   file that exercises `move_pipeline_stage`
   (`test_campaign_candidate_board.py`) mocks `PipelineTransitionService`
   out entirely, so its real `transition_stage()` body has never been
   run by a test. **Did not have the hook.** This is exactly how the
   live candidate above reached INTERVIEW with no `interview_schedules`
   row: a Pipeline Board drag-and-drop, SHORTLISTED -> INTERVIEW,
   `transition_source='MANUAL'`.
3. `CampaignService.override_candidate_stage()` (the "Stalled
   Candidates" manual-override action) - calls
   `CampaignRepository.transition_candidate_stage()` directly, a third,
   lower-level writer that neither of the other two engines wrap.
   `_STAGE_OVERRIDE_NEXT` maps `HM_REVIEW -> INTERVIEW` as a natural
   override target, and `target_stage` can also name `INTERVIEW`
   explicitly. **Also had zero test coverage** for this method, and
   **did not have the hook** either. Found by deliberately auditing for
   *every* writer of `pipeline_stage`, not by waiting for a third live
   incident - grepped `\.pipeline_stage = ` across `app/` and checked
   each hit's target stage(s) individually.

The common thread: the hook was added once, to the one class Epic 4's
own build prompt happened to name, without auditing whether it was
genuinely the only writer. It wasn't - both plain "does this call
transition_stage" instances would have caught it immediately, and the
absence of tests for both other classes is what let the gap ship
unnoticed in each case.

**How it was fixed:**
- Backfilled the newly-found live gap immediately
  (`InterviewScheduleRepository.get_or_create_pending`, same method,
  same live-verified zero-gaps-after check as the entry above).
- Added the identical hook (`if to_stage == PipelineStage.INTERVIEW:
  self.interview_schedule_repo.get_or_create_pending(...)`, same-
  transaction, before commit) to both `PipelineTransitionService.
  transition_stage()` and `CampaignService.override_candidate_stage()`.
- `PipelineTransitionService.interview_schedule_repo` is a **required**
  constructor param (same reversal `StageTransitionService` already went
  through) - `CampaignService.interview_schedule_repo` is optional,
  defaulted from `db` (matching that class's own existing convention for
  every other db-backed collaborator, e.g. `circuit_breaker_repo`), so
  the existing `get_campaign_service` DI wiring needed no change.
- Corrected `PipelineTransitionService`'s stale class docstring - it no
  longer claims to have zero callers.
- Added real tests for both classes' `to_stage=INTERVIEW` behavior
  (`test_pipeline_transition_service.py`,
  `test_campaign_service_override_candidate_stage.py`) - neither existed
  before this fix, which is precisely why the gap survived as long as it
  did in each case.
- Full sweep confirmed: exactly 3 places in `app/` ever assign
  `.pipeline_stage = <something>`; all 3 now carry the hook (the other 2
  hits from that grep - `campaign_candidate_repository.py`'s
  `update_pipeline_stage` and `override_revert_service.py`'s hardcoded
  `REJECTED` - are called by an already-covered engine or never target
  INTERVIEW at all, respectively).

**Not (this time) claiming "not an ongoing gap" without the same audit
that missed it twice already:** confirmed via the grep above, not
assumed - if a fourth writer is ever added, it needs this same hook
wired in as part of that change, not discovered later via another live
409.

---

## Test suite: widespread staleness from the `decision_*` model redesign

**Status:** open, discovered 2026-08-10, not fixed. Scope is large enough that
it needs its own pass, not a fold-in to whatever change happens to trip over it.

At some point before 2026-08-07, a merge (the same one that introduced the
`decision_type`/`decision_source`/`decision_reason`/`decision_details`/
`decision_by_user_id`/`decision_at` fields on `CampaignCandidate`, replacing
the older `ai_recommendation`/`hr_override_*`/`rejection_*` fields and the
standalone `RejectionLayer` enum/`CandidateRejectionRepository` module) was
never followed up with a pass over the test suite. Running
`tests/services/campaign/` + `tests/tasks/test_deterministic_scoring_tasks.py`
+ `tests/tasks/test_semantic_scoring_tasks.py` today: **97 failed, 6 collection
errors, 177 passed.**

Confirmed via representative sampling across 3 different files that this is
one root cause wearing different symptoms, not several unrelated problems:
- 6 files fail to even collect: `ImportError: cannot import name
  'RejectionLayer' from 'app.models.pipeline'` (the enum no longer exists).
- `test_deterministic_scoring_tasks.py` (32 of the 97 failures):
  `mock.patch("...CandidateRejectionRepository")` against a module that was
  deleted in the same redesign.
- `test_semantic_scoring_service.py` (12 of the 97): `SimpleNamespace`
  fixtures missing `deterministic_breakdown` and similar new fields — same
  "stale bare fixture" shape as the 3 fixtures fixed in
  `test_stage_transition_service.py` for E02 (see that file's git history),
  just not yet applied to every other file using the same fixture pattern.

**Not fixed here:** discovered while verifying E02's own fixture fixes
didn't leave anything else broken (they didn't — confirmed via `git status`
that none of the affected files were touched by E02's changes). Fixing this
properly means going through every stale fixture/mock across roughly 10
files and deciding, file by file, what the new correct fixture shape is —
a real, separate body of work, not a quick fix. Given how many files it
touches, whoever picks this up should probably do it as one dedicated pass
rather than patching files one at a time as they happen to get touched by
unrelated work (that's how the E02 test fixes ended up being only 3 of the
~15+ stale fixtures that actually exist).

---

## Test suite: `campaign_candidate.ai_evaluation` missing from stale `SimpleNamespace` fixtures

**Status:** open, discovered 2026-08-14, not fixed. Separate entry from
"Test suite: widespread staleness from the `decision_*` model redesign"
above — same general shape (a schema/model change landed without a
matching pass over test fixtures), but a different specific attribute,
a different (and wider) set of affected files, and not confirmed to share
the same root-cause merge, so kept distinct rather than folded in.

Found while verifying Epic 4 (M12) Step 3 (interview scheduling endpoints)
didn't introduce regressions: a full, non-scoped `pytest --collect-only`
followed by a full run (excluding the 6 files already known to fail
collection from the `RejectionLayer` issue above) produced **90 failed,
881 passed** — far outside Step 3's own change surface. Confirmed via
`git status`/`git log` that none of the failing files (or their
corresponding source files) were touched by this session's work.

Traced one representative failure
(`test_composite_scoring_service.py`) to its root cause:
`composite_scoring_service.py`'s `_effective_ai_score` reads
`campaign_candidate.ai_evaluation.effective_ai_score` (a 1:1 relationship
to `CampaignCandidateAIEvaluation`), but the test file's
`_make_campaign_candidate()` fixture builds a bare `SimpleNamespace` with
no `ai_evaluation` attribute at all, so every code path that reaches
`_effective_ai_score` raises `AttributeError:
'types.SimpleNamespace' object has no attribute 'ai_evaluation'` before
ever exercising the logic under test.

**Confirmed affected (by file, not exhaustively traced to root cause
beyond the one above):** `test_composite_scoring_service.py`,
`test_semantic_scoring_service.py`,
`test_campaign_candidate_ranking_service.py`,
`test_campaign_candidate_ranking_export_service.py`,
`test_campaign_candidate_rejection_analytics_service.py`,
`test_campaign_candidate_rejection_analytics_integration.py`,
`test_campaign_candidate_semantic_endpoint.py`,
`test_candidate_erasure_service_request_erasure.py` (tests/services/compliance/),
`test_jd_service_weight_assignment.py` (tests/services/jd/),
`test_resume_processing_pipeline_flow.py` (tests/services/resume/),
`test_embedding_tasks.py` (tests/tasks/) — several of these may share the
`ai_evaluation` root cause, others may not; not individually confirmed.
This is a wider file set than the `decision_*` redesign entry above ever
scanned (that entry only covered `tests/services/campaign/` plus 2 task
files), so it's plausible some of these were already broken before that
entry was written and simply never surfaced in a full run until now.

**Not fixed here:** out of scope for Epic 4 Step 3, which only touches
interview scheduling. Same "found something unrelated, didn't touch it,
documented it" standard as every other entry in this file. Whoever picks
this up should run a full, unscoped `pytest --collect-only -q` first
(not a targeted subset) to get an honest current count, since both this
entry and the `decision_*` entry above were each discovered by a
targeted run that didn't cover the other's files.

---

## Alembic: multiple unmerged migration heads

**Status:** open, pre-existing, recurring. Not fixed by any of the entries below.

`alembic heads` currently reports 5 unmerged leaf revisions instead of one:
`5439e70a5a8e`, `c4f8b2d6e1a3`, `d2a7c9e4f1b6`, `d88f97d9d5e0`, `f1a3c7e9b2d4`
(as of 2026-08-05). This means `alembic upgrade head` is ambiguous and will
refuse to run — any upgrade must target an explicit revision id instead.

The RDS dev database (`DB_HOST` in `.env`) is currently stamped at
`d2a7c9e4f1b6`, one of the 5 heads, so it is not "behind" — there's just no
single branch that represents the full schema history.

There's also a related but distinct instance of drift documented in
`docs/retry_mechanism.md` (a duplicate revision id `265912f5590a` plus several
migrations whose `down_revision` pointed at files no longer in the repo),
which made `alembic heads` fail outright at that time. Several
`*_placeholder_for_missing_revision.py` / `*_repair_*.py` migrations in
`alembic/versions/` exist specifically to paper over instances of this same
underlying pattern: someone applied a schema change directly against a shared
dev DB and hand-stamped `alembic_version` without ever committing the
migration file, orphaning whatever the "real" migration graph should have
been at that point.

**Prior mentions (scattered, not previously consolidated):**
- `docs/modules/M10-Candidate-Ranking.md` (composite scoring migration,
  `b1f4c9a2e7d3`) — branched off the most scoring-relevant head rather than
  merging every unrelated head.
- `docs/retry_mechanism.md` (JD retry/checkpoint migrations, `c71678d36109` /
  `4fd0a3c4f90d`) — hit the duplicate-revision-id variant of this problem.
- M12 workflow/interview-scheduling migration
  (`e686c750b7b4_email_trigger_enum_m12_events.py`) — branched off
  `d2a7c9e4f1b6`, the current RDS head, same pattern as above. See that
  migration's docstring.

**Standing workaround until someone actually merges the graph:** new
migrations branch off whichever existing head is most relevant to the work at
hand (verified live via `alembic current` against the target DB, not assumed
from the versions directory), are applied via an explicit revision id
(`alembic upgrade <revision>`, never bare `head`), and get a short docstring
note pointing back here.

**Real fix, not yet scheduled:** someone needs to sit down with all the heads,
determine which ones (if any) represent schema changes never actually applied
anywhere live, and author real merge migration(s) — the same pattern already
used by `a558bcbcdb92_merge_all_outstanding_heads.py`,
`c71678d36109_merge_current_heads_for_jd_retry_checkpoint_support.py`,
`2c82aaa93c9f_merge_migration_heads.py`, and
`3e7800c51995_merge_resume_skill_ontology_bulk_upload_.py` — to collapse back
to one head. Whoever owns migration history should be looped in before that
work starts, since it touches every branch, not just the newest one.

**2026-08-07 recurrence:** after a `git merge origin/main` (teammates'
niharika/sathwik/loki/RMe branches) brought in 6 new migrations, each
branching off one of the original 5 heads (`d88f97d9d5e0` forked into two:
`535cfe5721bb` and `e4a9c1f6b8d3`), followed by an RDS dev-DB reset/restore,
`alembic current` failed outright: `alembic_version` was stamped at
`7043b9ed5abe`, a revision that has never existed as a file anywhere in this
repo's git history (confirmed via `git log --all -- "*7043b9ed5abe*"` —
empty). Table-level schema was fully intact (all 43 tables present), but at
least one enum type (`email_trigger_event_enum`) was missing our M12
migration's 4 values at the *type* level, not just the data level — meaning
the live schema state didn't match any single revision in this repo's
history, most likely because someone applied all 6 new-head migrations via
`alembic upgrade` on a checkout that had an uncommitted 7th merge-migration
file, and that database (or a snapshot of it) is what RDS was reset from.

**How it was fixed this time:** verified live, per migration, which of the 6
new heads' actual schema effects (specific enum values, a specific added
column) were genuinely present on RDS — all 6 were. Rather than guess a
single revision to stamp to, used `alembic stamp --purge <all 7 branch
tips>` (the 6 verified-live heads plus our own migration's parent,
`d2a7c9e4f1b6`) to correctly represent multiple simultaneously-current
independent branches, then `alembic upgrade e686c750b7b4` to bring our own
branch forward by one. First attempt at the stamp missed the
`535cfe5721bb`/`e4a9c1f6b8d3` sibling-fork pair (only included one of the
two), silently dropping a valid head — caught by comparing `alembic current`
against `alembic heads` after stamping (they must match) and re-stamping
with the full corrected set. Worth remembering: when a fork exists (one
parent, multiple children, all independently verified live), **every**
branch tip needs including in the stamp — including one but not its
sibling produces a `current` set that looks successful but silently drops a
branch.

**2026-08-10 recurrence:** while building the E02 (Stage Transition Rules &
Enforcement) migration adding `campaign_candidate_stage_history.
idempotency_key` (`08655d0b0117`), `alembic current` failed again:
`alembic_version` was stamped at `9a1c2f3e6b7d`, a revision that doesn't
exist anywhere — not in local `alembic/versions/`, not in `origin/main`
(fetched and checked directly), not in any teammate branch. Verified before
doing anything: table-level schema on the target campaign_candidate_stage_history
table was completely unaffected (same 9 columns, same single PK index as
the last audit), and both of this repo's then-current heads' schema effects
(`09f831e39061`'s `audit_entity_type_enum` values, `e686c750b7b4`'s
`email_trigger_event_enum` values) were confirmed still live.

**Root cause: confirmed 2026-08-11 (was previously logged as unconfirmed).**
This was never a phantom/corrupted stamp — it was a **coincidental
revision-id collision** with a teammate's real, legitimately-applied
migration. `9a1c2f3e6b7d_add_rejection_composite_trigger.py` (adds
`REJECTION` to `composite_score_trigger_source_enum`, `down_revision =
7043b9ed5abe`) genuinely existed and had genuinely been run via `alembic
upgrade` against the shared dev DB — which is exactly what produced the
`9a1c2f3e6b7d` stamp this entry originally called unexplained. At the time
this was investigated (2026-08-10), that file was checked for and not
found anywhere (local, `origin/main`, every fetched teammate branch) — an
accurate result at that moment, since the file had not yet been merged
into this checkout. It arrived in a later merge, and because
alembic-generated revision ids are short random hex strings with no
collision detection across parallel branches, it happened to land on the
exact same 12-hex-char id (`9a1c2f3e6b7d`) that the placeholder file below
had already claimed. `alembic heads` then started emitting `Revision
9a1c2f3e6b7d is present more than once`, and `alembic history`/`alembic
current` failed outright once both files coexisted in the same checkout.

**How it was fixed the first time (2026-08-10):** authored a no-op
placeholder migration (originally `9a1c2f3e6b7d_placeholder_for_missing_revision.py`)
merging the 2 then-current heads (`09f831e39061`, `e686c750b7b4`) into the
exact stamped revision id, matching the same pattern as the prior
recurrence, then chained the real `idempotency_key` migration on top.
Confirmed clean at the time — but the teammate's colliding file had not
yet been pulled into this checkout, so the collision wasn't visible yet.

**How the collision was fixed (2026-08-11):** the placeholder file was
renamed to a freshly-generated, verified-unique id (`b6dda6ad1824`) — the
teammate's real migration keeps its original id `9a1c2f3e6b7d` untouched,
since it's already-merged work with its own `down_revision` chain
(`7043b9ed5abe`) that others may depend on; renaming a real migration
after the fact is the more dangerous direction. The renamed placeholder's
`down_revision` was extended from a 2-way merge to a 3-way merge —
`('09f831e39061', 'e686c750b7b4', '9a1c2f3e6b7d')` — since the teammate's
migration is a third independent sibling fork off the same
`7043b9ed5abe` root, and its schema effect (`REJECTION` in
`composite_score_trigger_source_enum`) was re-confirmed live before adding
it as a merge parent, same "verify every branch tip" methodology as every
prior recurrence. `08655d0b0117`'s `down_revision` was updated to point at
the renamed id. Confirmed clean afterward: `alembic heads` resolves to
exactly one head (`08655d0b0117`) with zero warnings, and `git diff` on
the teammate's file shows zero changes — only the placeholder (renamed)
and `08655d0b0117` (one line, `down_revision`) were touched.

`allowed_transitions` (26 rows) and `campaign_candidate_stage_history` (41
rows, all with `idempotency_key IS NULL`) were unchanged before/after the
original 2026-08-10 fix.

**Note for whoever reads this next:** `alembic current` still fails today,
but with a *different*, unrelated error — see the new entry below
(`alembic_version` phantom-stamped at `d3a86f21c9e4`). Do not conflate the
two: the `9a1c2f3e6b7d` collision above is fully resolved at the file
level; `d3a86f21c9e4` is a fresh, separate, still-open incident.

---

## Alembic: `alembic_version` stamped at a phantom revision `d3a86f21c9e4` (2026-08-11)

**Status:** resolved 2026-08-13. Discovered as a side effect of verifying
the `9a1c2f3e6b7d` collision fix above (`alembic current` was expected to
succeed once that collision was resolved; instead it failed with a
different, unrelated error).

`alembic_version` was stamped at `d3a86f21c9e4` — a revision id that did
not exist anywhere in this repo's `alembic/versions/` directory at the
time (confirmed: only 6 migration files existed in that checkout,
`7043b9ed5abe`, `09f831e39061`, `e686c750b7b4`, `9a1c2f3e6b7d`,
`b6dda6ad1824`, `08655d0b0117` — `d3a86f21c9e4` was not one of them).

**Investigated (2026-08-11), before the real file was known:** a full
drift audit (recursively importing every SQLAlchemy model and comparing
all 43 live tables and all 34 live enum types against the ORM) found
exactly 3 unexplained items — `hiring_campaigns.max_missing_core_skills`,
`hiring_campaigns.required_skill_coverage_threshold`, and
`jd_skills.importance` (new enum `jd_skill_importance_enum`) — all live,
all referenced by zero code anywhere in `app/`. Concluded these were
"in-progress work applied directly to the shared dev DB, migration file
never committed," and authored a no-op placeholder
(`d3a86f21c9e4_placeholder_for_missing_revision.py`) into the exact
stamped id, explicitly stating *"this is not a collision with a real
migration that simply hadn't been pulled yet."*

**That statement was wrong, and it's worth naming plainly why, since it's
a different mistake from the `9a1c2f3e6b7d` incident above and worth
telling apart:** the `9a1c2f3e6b7d` case was a genuine *coincidental*
collision — two people independently generated the same random 12-hex-char
id at roughly the same time, with no way either could have known about the
other's revision beforehand. This case was not that. The real migration
(`d3a86f21c9e4_skill_importance_and_qualification_thresholds.py` —
confirmed on merge, 2026-08-13, to add exactly the 3 items found above)
already existed on a teammate's branch (niharika, PR #94) at the time of
the 2026-08-11 investigation — it simply hadn't been merged into this
checkout yet. "No file exists anywhere I can find" was an accurate
statement about that moment, not a permanent fact — especially on a repo
with this much concurrent, unpushed, or in-flight work across branches.
The exhaustive-sounding search (recursive model audit, live schema diff)
made the conclusion *feel* more certain than "phantom, not a pulled-later
collision" actually was; it was still bounded by what existed in fetched
branches at that moment. Worth remembering next time a search comes back
empty: empty now doesn't mean empty later.

**How it was fixed (2026-08-13, once the real file landed via merge):**
same resolution shape as the `9a1c2f3e6b7d` incident — the placeholder
(never anything but a no-op) was deleted outright rather than renamed,
since alembic_version only ever stores a revision id string, never a file
hash. The live DB was already stamped past `d3a86f21c9e4` (at
`7b3f6a92e1c4`, downstream), so alembic never re-runs a revision it has
already recorded as passed — swapping which file backs an already-passed
id is safe *specifically because* it's already passed; this would not
hold for a revision the DB hadn't reached yet. Using the real migration
file going forward (instead of the deleted no-op) is also the correct
choice for any future fresh-database bootstrap, since the placeholder
never actually created these columns — only the real file does.
`c8e1a4f97d52`'s `down_revision` already read `'d3a86f21c9e4'` and needed
no change; deleting the placeholder alone let the chain resolve onto the
real file. Confirmed clean afterward: `alembic heads`/`current` no longer
warn about this id at all.

**Uncovered while verifying this fix, tracked separately below:** a
*third*, unrelated head (`c1f4a7b93e20`, M11 saved views/skill search,
also from PR #94) was hidden by this collision's noise and only became
visible once it was resolved — see the next entry.

---

## Alembic: `c1f4a7b93e20` (M11 saved views/skill search) is a second unmerged head (2026-08-13)

**Status:** resolved 2026-08-13. Surfaced only after deleting the
superseded `d3a86f21c9e4` placeholder above — with two migrations both
claiming `d3a86f21c9e4`, `alembic heads` was already erroring out on that
collision and never got far enough to report this second, independent
problem.

`c1f4a7b93e20_m11_saved_views_and_skill_search.py` (PR #94, adds the
`user_saved_views` table, `candidate_notes` table, `search_queries.
canonical_skill_ids`, and 3 `audit_action_type_enum` values) has
`down_revision = '7043b9ed5abe'` — it branches directly off the initial
schema migration, not off this branch's current tip at the time
(`7b3f6a92e1c4`). Same fork shape as every recurrence above, structurally
— but worth naming plainly what's actually different about the *cause*
here, distinct from both incidents above:

- `9a1c2f3e6b7d` was a genuine **coincidental hash collision** — two
  people independently generated the same random id with no way either
  could have known about the other.
- `d3a86f21c9e4` was **a premature conclusion** — the real file existed
  on a teammate's branch the whole time; the investigation just hadn't
  fetched it yet, and said so more confidently ("not a collision... this
  is genuinely missing") than the evidence actually supported.
- **This one is neither a collision nor a mistake.** Confirmed initially
  (2026-08-13, first pass) that neither `user_saved_views` nor
  `search_queries.canonical_skill_ids` existed live yet — a genuinely
  new, not-yet-applied migration, nothing to investigate further. By the
  time the fix was actually written moments later, a **concurrent
  process — almost certainly a teammate or CI finishing the same PR #94
  merge from another environment** — had run `c1f4a7b93e20` for real
  against this same shared dev DB: `user_saved_views` now existed,
  `alembic_version` was restamped to `c1f4a7b93e20` alone, with no
  record that this branch's own chain (`7b3f6a92e1c4` and everything
  under it) had ever been applied — even though every one of its schema
  effects (audit_log indexes/triggers, `idempotency_key`, etc., re-
  verified directly against the live DB) was still fully intact. Nothing
  was lost; only the bookkeeping briefly disagreed with reality. **This
  is arguably the most normal and likely-to-recur of the three risks
  documented in this file** — two people validly working on two
  different branches of the same migration chain at the same time on a
  shared instance is not a bug in anyone's workflow, it's the default
  outcome of that workflow without a lock. It's the strongest case yet
  for "always `git fetch` and check `alembic heads` immediately before
  starting new migration work, and again immediately before stamping/
  upgrading" — not because someone erred, but because this can happen
  even when nobody does.

**How it was fixed:** re-verified live (full recursive model-vs-DB drift
audit, not just the two originally-named items) that both branches'
complete schema effects were genuinely live before touching anything.
Authored a real merge migration (`43535e9e3cf7`, `down_revision =
('7b3f6a92e1c4', 'c1f4a7b93e20')`) and applied it via `alembic stamp`
(not `upgrade` — by that point nothing needed to execute; both sides
were already applied). Confirmed clean afterward: `alembic heads`/
`current` resolve to the single new head with no warnings.

---

## Alembic/schema: undocumented "scheduled exports / compliance" drift (2026-08-13)

**Status:** resolved (bookkeeping only — the feature itself is not built).
Discovered while verifying the `c1f4a7b93e20` merge above was safe to
stamp: the full recursive model-vs-live drift audit run for that merge
turned up further drift with an unrelated root cause, deliberately kept
as its own entry rather than folded into the merge above — different
cause, different fix, easier for a future reader to follow separately.

Found live, with zero corresponding file or code anywhere:
- `hiring_campaigns.scheduled_export_config` (jsonb, nullable)
- 8 new `audit_action_type_enum` values: `AUDIT_TRAIL_EXPORTED`,
  `CANDIDATE_LIST_EXPORTED`, `COMPLIANCE_REPORT_EXPORTED`,
  `DSAR_EXPORTED`, `SCHEDULED_EXPORT_CONFIGURED`, `SCHEDULED_EXPORT_SENT`,
  `SCORECARD_EXPORTED`, `SHORTLIST_PACKAGE_EXPORTED`

Reads like one cohesive, unbuilt feature (DSAR = Data Subject Access
Request, a GDPR term) — a "compliance / scheduled exports" epic that
someone applied directly to this shared dev DB.

**Searched exhaustively before concluding this is genuinely missing** —
learning directly from the `d3a86f21c9e4` mistake above, not just
repeating the same search with more confidence: `git fetch --all` first
(picked up brand-new commits on `RMe`/`loki`/`main`/`niharika`), then
sanity-checked that the search mechanism itself actually works by
confirming it *does* find a known-real term (`c1f4a7b93e20` on
`origin/main`) before trusting a "zero results" reading for the terms
that actually matter, then searched every local and remote branch tip,
current and full history (`git log --all --diff-filter=A`). Zero hits
anywhere, on anything.

**How it was fixed:** authored a no-op placeholder (`e9961d228f3d`,
`down_revision = '43535e9e3cf7'`, chained on top of the merge above) and
applied it via `alembic stamp` (not `upgrade` — the schema change is
already live; nothing to execute). This resolves the bookkeeping only.
**The feature itself does not exist** — no model field, no service, no
endpoint reads or writes `scheduled_export_config` or any of the 8 new
action types anywhere in this codebase. Whoever picks up the real
"compliance / scheduled exports" work will be adding code against a
schema that already exists, not authoring the schema itself.

---

## `StageTransitionService.transition_on_ai_success` has zero test coverage

**Status:** open, discovered 2026-08-11, not fixed. Worth its own ticket —
out of scope for E02.

`transition_on_ai_success` (`app/services/campaign/stage_transition_service.py:
116-179`) is real, live, and correctly wired — called from 2 sites in
`app/tasks/ai_evaluation_tasks.py` (the `SHORTLIST` and `HOLD` recommendation
branches of AI evaluation), and correctly backs the `SCREENING -> HOLD` and
`SCREENING -> SHORTLISTED` `allowed_transitions` rows. **Confirmed not
orphaned** — an earlier internal investigation note (during E02's Step 0
audit) concluded this method didn't exist anywhere in the codebase and that
the `SCREENING -> HOLD` row was orphaned; that conclusion was based on an
incomplete grep run before this method (and its caller in
`ai_evaluation_tasks.py`) landed via a later teammate merge (PR #90, "RMe"
branch). That note was never written to this file, only stated in chat, so
there's nothing to retract here beyond this correction.

**The actual gap:** zero test coverage, anywhere. Confirmed via a
file-reference grep for `transition_on_ai_success` across the whole repo —
exactly 3 files reference it: `app/tasks/ai_evaluation_tasks.py` (the 2 call
sites), `app/services/campaign/stage_transition_service.py` (the method
itself), and `app/seeds/seed_allowed_transitions.py` (descriptive `notes`
text on the 2 rows it backs). No test file references it at all — not
`tests/services/campaign/test_stage_transition_service.py` (which does test
its sibling `transition_to_screening`), not anywhere else. This method
moves real candidates into `HOLD`/`SHORTLISTED` pipeline stages in
production, with nothing catching a regression.

**Not fixed here:** out of scope for E02, which only added the new
`transition()` method — `transition_to_rejected`/`apply_hr_override`/
`transition_to_screening`/`transition_on_ai_success` are all pre-existing
and untouched by E02's changes.

---

## Epic 5: `MAX_EMAIL_RETRY_COUNT` (seeded, value 4) vs. the actual hardcoded retry cap (3)

**Status:** open, discovered 2026-08-18 during Epic 5 Step 0's investigation.
Real, minor inconsistency — not fixed as part of Step 2.

`seed_platform_config.py` seeds `MAX_EMAIL_RETRY_COUNT = "4"`, described as
"Max attempts for a transient interview/notification email send failure
before dead-lettering." Nothing in the codebase actually reads this config
key — `app/tasks/email_tasks.py`'s `send_candidate_email_task` uses its own
hardcoded `_EMAIL_RETRY_POLICY = RetryPolicy(max_attempts=3, ...)` instead,
unrelated to the seeded value. Confirmed via a repo-wide grep for
`MAX_EMAIL_RETRY_COUNT` — the seed row is the only reference anywhere.

Practical impact is small: email sends really do cap at 3 attempts before
dead-lettering, not 4, and nothing currently depends on the seeded value
being authoritative. Worth fixing eventually (either read the config value
into `_EMAIL_RETRY_POLICY.max_attempts` at task start, or delete the unused
seed row so it stops implying a behavior that isn't real), but out of scope
for Epic 5 Step 2's recipient-model widening.

---

## Epic 5: real, built infrastructure with zero current callers - not bugs, deliberately forward-scoped

**Status:** informational, not a defect. Naming the pattern explicitly
because it's now happened twice with the same shape, and whoever picks up
the feature each of these is actually waiting on should know the
groundwork already exists rather than re-discovering or re-building it.

- **`SHORTLIST_NOTIFICATION_BATCH_WINDOW_MINUTES`** (seeded platform
  config, M12) - describes batching `SHORTLISTED` notifications into a
  digest. No `SHORTLISTED` trigger event exists in `EmailTriggerEvent`,
  and no digest/batching mechanism exists anywhere in this codebase.
  Waiting on: whatever eventually decides shortlist notifications should
  exist at all, and in digest form specifically.
- **`user_notification_preferences`** (table + `is_notification_enabled()`
  helper + minimal `GET`/`PUT /users/me/notification-preferences` API,
  Epic 5 Step 3) - fully built and tested, but nothing calls
  `is_notification_enabled()` yet. Confirmed during Step 3's own
  investigation: of the 6 real `EmailTriggerEvent` values, the 5 with a
  live send path today (`CANDIDATE_REJECTED`, `INTERVIEW_SCHEDULED`/
  `RESCHEDULED`/`CANCELLED`, `CANDIDATE_SELECTED`) all target a candidate
  or an external interviewer - neither has a `users.id` row to hold a
  preference against. `UPLOAD_PERMANENTLY_FAILED` is the one trigger
  event actually scoped for internal users (the uploader + all active
  HR_ADMIN), but it has zero send path of its own (see the
  `UPLOAD_PERMANENTLY_FAILED`-precedent comment in `app/models/email.py`
  - that's D11, still unbuilt). Waiting on: D11 (or any other future
  internal-user-facing trigger). Whoever builds that should know this
  table and helper are already sitting there ready, not something to
  design from scratch.

Both were deliberately NOT forced into a fake integration just to prove
they're wired - an honest "built, not yet consumed" beats a contrived
caller that would need undoing later. If a third instance of this same
shape turns up, it's worth asking whether trigger-event infrastructure is
systematically being scoped ahead of the features that will use it, or
whether that's simply an accurate reflection of this epic's own staged
build order.

---

## Interview scheduling: historical rows' real timezone is unrecoverable (fixed going forward 2026-08-24)

**Status:** fixed for every schedule()/reschedule() call from this date
forward; one residual gap in historical data that cannot be corrected by
code.

Until this fix, `ScheduleInterviewRequest`/`RescheduleInterviewRequest`
had no timezone field at all - `_combine_utc()` just tagged whatever raw
date/time the client sent with `tzinfo=UTC`, a relabel, not a conversion.
Every downstream reader (the Teams/Google calendar invite payload, the 3
notification email builders, the interviewer feedback form) then echoed
those mislabeled numbers, so they all agreed with each other internally
but none were actually correct - and `request_feedback()`/`complete()`'s
"has this interview happened yet" gates and the feedback-request sweep
compared this fake-UTC value against a real `datetime.now(timezone.utc)`,
firing up to a full UTC-offset early/late. Reported live as a mail-vs-
calendar time discrepancy - the calendar invite is auto-localized by the
viewer's own calendar client (which is why it looked "right" to some
viewers and "wrong" to others), while the static email text was never
localized to anything.

Fixed: `interview_schedules.timezone` (new column, IANA zone name) is now
required on the request schema, and `_combine_to_utc()` does a genuine
`ZoneInfo`-based conversion before storage. Every reader that used to
format `start_at`/`end_at` directly now converts back to `schedule.
timezone` first (`InterviewScheduleService._to_response`,
`InterviewFeedbackService.get_feedback_form_context`,
`candidate_notification_emails._interview_email_context`,
`interview_interviewer_lifecycle_emails._round_context`). The calendar
invite payload needed no change - once the underlying instant is
genuinely UTC, declaring `"timeZone": "UTC"` in the Graph/Google payload
is already correct.

**The one thing this cannot fix:** rows created before this migration
have `timezone` backfilled to `'UTC'` as a technical column default, not
a claim that they were actually scheduled in UTC - their real intended
timezone was never recorded anywhere and cannot be recovered. Any
still-active historical round scheduled by a non-UTC caller will keep
displaying whatever wrong time it already had. This only matters for
rounds still SCHEDULED/RESCHEDULED (not yet COMPLETED/CANCELLED) as of
2026-08-24; no backfill/correction script is possible without knowing
what timezone each one actually meant.
