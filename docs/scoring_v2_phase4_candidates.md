# Scoring v2 — Phase 4 candidates

Ideas found during Phase 3 that were deliberately not built. Each needs an
evaluation on real resumes before it is approved.

## Clause splitting for paraphrase lines (noted 2026-10-22)

**What:** split each candidate line the paraphrase step searches into clauses
(at `;`, and possibly ` - ` or ` | `) and score every clause on its own,
instead of the whole line.

**Why it came up:** Phase 3 F2 (Junior Python Developer, Fresher template).
F2's only Git evidence is one clause at the end of a long project line:

> Campus Events API: Designed and built a REST API in Python with Flask;
> stored events in PostgreSQL with hand-written SQL queries;
> **version-controlled on GitHub with pull requests from two teammates.**

Two Step 6 rules block it:

- similarity of the whole line to Git is 0.255 (threshold 0.45). The line is
  mostly about REST, Python and SQL.
- the line is already quoted as evidence for those other core skills, so it
  is excluded ("evidence for another core skill").

The Git clause on its own would likely score well above the threshold. It
would also no longer be the quote for any other skill.

**What fixed F2 instead (Phase 3):** an implied-skill rule in
`skill_pattern_rules` (GitHub / GitLab / Bitbucket → Git, DERIVED 0.8, always
voted on). It covers this one wording family, not paraphrase in general.

**To evaluate before building:**

- Run on real resumes (not the synthetic Phase 3 fixtures). Compare the
  proposals and votes with and without splitting, using the
  `scripts/paraphrase_eval.py` dry run.
- Watch for new false proposals: short clauses ("led a team of 5") may match
  many skills.
- Check the interaction with rule 2 (a line already evidence for another core
  skill). Splitting makes clause-level containment the test, so decide
  whether a clause inside a quoted line may still be proposed.
- Check the LLM-call cost: more candidate units means more proposals and
  more votes against the daily cap.
