# Scoring v2 — Phase 4 candidates

Ideas found during Phase 3 that were deliberately not built. Each needs an
evaluation before it is approved. Since 2026-10-30 every phase is built and
evaluated in dev on synthetic data only (see the [roadmap](roadmap.md)). So the
evaluation runs on the golden set with the golden runner, extended with
synthetic cases that show the problem, not on real resumes.

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

- Run on the golden set, extended with synthetic resumes that have long
  multi-clause lines. Compare the proposals and votes with and without
  splitting (the golden runner, or the `scripts/paraphrase_eval.py` dry run).
- Watch for new false proposals: short clauses ("led a team of 5") may match
  many skills.
- Check the interaction with rule 2 (a line already evidence for another core
  skill). Splitting makes clause-level containment the test, so decide
  whether a clause inside a quoted line may still be proposed.
- Check the LLM-call cost: more candidate units means more proposals and
  more votes against the daily cap.

## One line confirming two core skills; the best line for a third already used (noted 2026-10-26)

**What happens:** senior test set, S2 ("Head of Data & AI Platforms").
Machine Learning auto-confirms from a cloud-architecture line:

> Designed the Azure cloud architecture for the data and ML platform

That line is S2's first Machine Learning scan line. "ML" counts as Machine
Learning next to a cue word ("platform"), and the line leads with a work
verb. The same line also auto-confirms Cloud Architecture. It mentions ML
only in passing; the line that really shows ML work comes second:

> Built the first ML feature pipelines with the data science team

MLOps is then left with no confirmable line. S2 lists it only in the skills
list (listed-only, so UNCERTAIN). In the paraphrase search its strongest
experience line is the ML feature-pipeline line (0.487), which is excluded
because it is already Machine Learning evidence. The next one, "Deployed
fraud-detection models to production with Azure ML, with monitoring and
scheduled retraining", scores 0.410, below the 0.45 threshold.

S2's outcome is right (HOLD, for experience 0.9 years short). But it depends
on Machine Learning being confirmed from a neighbouring skill's line: without
it S2 would most likely be REJECT. The ML rule was left as it is (decision
2026-10-26).

**Related, same candidate:** Data Engineering is confirmed by the vote on a
title line ("Head of Data Engineering — Orbit Payments · Oct 2021 – present").
The paraphrase step skips title lines; the scan evidence and the vote do not.

**Ideas to evaluate:**

- Choose, for each core skill, the line that is most about that skill (for
  example by similarity to the skill description plus the JD's requirement
  text), rather than the first scan line that qualifies.
- Allow one line to confirm several core skills only when it is clearly about
  each of them, or let a line excluded for "evidence for another core skill"
  still be proposed when it is a much better match for the searched skill.
- Treat a work-history title line as weak evidence: never auto-confirmed and
  shown to the voters as a title.
- Evaluate on the golden set, as for clause splitting.
