# Known Issues

Tracked structural/process issues that are known, understood, and deliberately
not fixed in the change that discovered or re-encountered them. Each entry
should stay short: what's wrong, where it's been seen, and what the standing
workaround is until someone actually fixes it.

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

**Root cause: unconfirmed.** Unlike the 2026-08-07 recurrence, no plausible
mechanism was identified this time (no relevant upstream merge, no dangling
down_revision from a squash) — this entry documents that the phantom stamp
existed and was worked around, not why it existed. Do not read this as
"understood," only as "papered over safely."

**How it was fixed this time:** authored a no-op placeholder migration
(`9a1c2f3e6b7d_placeholder_for_missing_revision.py`) merging the 2
then-current heads (`09f831e39061`, `e686c750b7b4`) into the exact stamped
revision id, matching the same pattern as the prior two recurrences, then
chained the real `idempotency_key` migration on top. Confirmed clean
afterward: `alembic current` and `alembic heads` both resolve to the single
new head (`08655d0b0117`), and the migration itself was purely additive —
`allowed_transitions` (26 rows) and `campaign_candidate_stage_history` (41
rows, all with `idempotency_key IS NULL`) were unchanged before/after.
