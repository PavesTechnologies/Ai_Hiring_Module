# Known Issues

Tracked structural/process issues that are known, understood, and deliberately
not fixed in the change that discovered or re-encountered them. Each entry
should stay short: what's wrong, where it's been seen, and what the standing
workaround is until someone actually fixes it.

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

**Real fix, not yet scheduled:** someone needs to sit down with all 5 heads,
determine which ones (if any) represent schema changes never actually applied
anywhere live, and author real merge migration(s) — the same pattern already
used by `a558bcbcdb92_merge_all_outstanding_heads.py`,
`c71678d36109_merge_current_heads_for_jd_retry_checkpoint_support.py`,
`2c82aaa93c9f_merge_migration_heads.py`, and
`3e7800c51995_merge_resume_skill_ontology_bulk_upload_.py` — to collapse back
to one head. Whoever owns migration history should be looped in before that
work starts, since it touches every branch, not just the newest one.
