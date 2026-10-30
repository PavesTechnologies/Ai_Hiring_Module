# Golden set

The synthetic candidates scoring is checked against. They're self-contained: everything needed to score a case is in this folder, so dev's database and the parser aren't needed. The format is described in `scripts/golden/golden_format.py`.

| Set | Cases | JD |
|---|---|---|
| phase3 | F1–F8, D1–D8 | JD-F (Junior Python Developer), JD-D (ServiceNow ITSM) |
| hard_negative | H1–H10 | JD-F, JD-D |
| senior | S1–S8 | JD-S (Head of Data & AI Platforms) |
| counterfactual | CF-… | as their base |

- `golden_set.json` holds the cases and their expectations: the accepted outcomes and, where exact, the core count.
- `jds/<key>.json` holds a JD: its row, calibration, JD skills, unknown skills and campaign settings.
- `resumes/<id>.json` holds a frozen parse: the redacted text, `parsed_json`, the matched skills and the resume row.

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
4. Run `pytest tests/golden`.

## Refreezing

`python -m scripts.golden.refreeze --only <ids>` parses the `.docx` again with the app's parse stages and shows the differences from the frozen file before anything is written:
- text lines;
- parsed fields;
- per entry, the fields that changed or were added empty;
- matched skills.

`--write` saves it. Refreeze deliberately: a new parse changes what the golden runner scores.

The 34 original cases were frozen from dev's stored parses with `python -m scripts.golden.freeze_from_dev`. F1 is `tests/fixtures/duplicates/F1_v2.docx`, the version dev has scored.
