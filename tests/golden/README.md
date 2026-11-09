# Golden set

The synthetic candidates scoring is checked against. They're self-contained: everything needed to score a case is in this folder, so dev's database and the parser aren't needed. The format is described in `scripts/golden/golden_format.py`.

| Set | Cases | JD |
|---|---|---|
| phase3 | F1–F8, D1–D8, ONE-SPLIT | JD-F (Junior Python Developer), JD-D (ServiceNow ITSM), JD-ONE (JD-F with SQL as its only core skill) |
| hard_negative | H1–H10 | JD-F, JD-D |
| senior | S1–S8 | JD-S (Head of Data & AI Platforms) |
| counterfactual | CF-… | as their base |

- `golden_set.json` holds the cases and their expectations: the accepted outcomes and, where exact, the core count.
- `jds/<key>.json` holds a JD: its row, calibration, JD skills, unknown skills and campaign settings.
- `resumes/<id>.json` holds a frozen parse: the redacted text, `parsed_json`, the matched skills and the resume row.

## Running it

```
python -m scripts.golden.run                   # all cases; the model is called only on a vote-cache miss
python -m scripts.golden.run --only S1 H7      # some cases
python -m scripts.golden.run --cache-only      # no model call at all: a miss fails (what CI can run)
python -m scripts.golden.run --dev-cache       # on a miss, reuse dev's cached votes first
python -m scripts.golden.run --accept-run tests/golden/runs/<stamp>.json   # accept a reviewed run, as it was
```

**What a run does:**
- Each case's JD, campaign, candidate and resume rows are built from the frozen files inside one database transaction, which is always rolled back.
- Evidence is detected from the frozen text and parse.
- Scoring v2 runs as in the app.
- The configuration is the connected database's: dev, or a fresh database built with migrations and the seed (point `DB_*` at it).

**What the report shows:** each run writes `runs/<stamp>.md` and `.json`, which aren't committed:
- expectations met or failed;
- each counterfactual pair, same or different, with the cause;
- changes since the accepted run, per core skill, with causes: evidence lines, auto-confirm or vote, votes, paraphrase;
- LLM calls and tokens.

**Votes:** they come from `vote_cache.json`, keyed like the app's confirmation cache, including the model.

**The merge check:** `accepted_run.json` is the last accepted run. It records the scoring fingerprint: a hash of the scoring and detection code, the prompt, the seed data and this set's cases and frozen files (`scripts/golden/fingerprint.py`). `test_accepted_run.py` fails whenever the fingerprint has changed. So a change to scoring must come with a new run, reviewed and accepted. Editing expectations or notes doesn't change the fingerprint.

The exact core counts in `expected.core` are kept only where the designed count equals the accepted result. Elsewhere the designed count is in the note: cases accepted by outcome (D2, H2, S1–S3, reconciled 2026-10-30).

## Counterfactual pairs

A counterfactual case is a copy of a base resume that differs only in:
- the candidate's **name** (spanning gender and community);
- a **career gap** (left silent, or stated as a career break);
- the **region**.

It must get the same outcome and core-skill results as the case named in `compare_with`.

Each group has a **control**: the base unchanged, parsed fresh together with its variants. The variants are compared with the control. The control is compared with the original case, so any difference there is parse drift, not fairness.

The resumes are built by `scripts/fixtures/build_counterfactuals.py`. All names are synthetic.

## Adding a case

1. Put the resume `.docx` under `tests/fixtures/`.
2. Add the case to `golden_set.json`.
3. Freeze its parse: `python -m scripts.golden.refreeze --only <id>`. This shows the parse; add `--write` to save it. It makes one LLM call per resume.
4. Run the golden set, review the report, and accept the run with `--accept-run`. A new case changes the fingerprint.
5. Run `pytest tests/golden`.

## Refreezing

`python -m scripts.golden.refreeze --only <ids>` parses the `.docx` again with the app's parse stages and shows the differences from the frozen file before anything is written:
- text lines;
- parsed fields;
- per entry, the fields that changed or were added empty;
- matched skills.

`--write` saves it. Refreeze deliberately: a new parse changes what the golden runner scores.

The 34 original cases were frozen from dev's stored parses with `python -m scripts.golden.freeze_from_dev`. F1 is `tests/fixtures/duplicates/F1_v2.docx`, the version dev has scored.

**Pinned votes:** ONE-SPLIT's vote-cache entry is edited by hand to a split vote (yes, no, yes) - the live model said yes three times. It checks that an undecided core skill never rejects (review item 24); its entry carries a `note` saying so. Re-recording the cache must keep it.
