# Roadmap

The agreed order (2026-10-30):

1. Dev data cleanup - **done**
2. Production configuration seed - **done**
3. Senior test set - **done**
4. Phase 4: golden set and checks - **done** (2026-11-02)
5. Retire v1 - **next**
6. Go to production

**Every phase is completed in dev on synthetic data. Only the finalised system goes to production.**

## 1. Dev data cleanup - done (2026-10-26)

Dev holds synthetic data only. The six real candidates, their campaign and their JD were erased through the app's erasure path. The audit entries kept for them were cleaned of personal data (approved trigger bypasses). The gaps found while doing it were fixed:
- erasure now covers stage rows and the parse cache;
- `resumes.parse_text_hashes` was added;
- audit entries hold no personal data, with a guard test;
- the `audit_log.campaign_id` foreign key was dropped.

What remains in dev: the 26 [P3] candidates (Phase 3 F1–F8, D1–D8; hard negatives H1–H10) and the 8 [SENIOR] candidates (S1–S8).

## 2. Production configuration seed - done (2026-10-26, updated 2026-10-30)

`app/seeds/product_config/` holds the configuration a fresh production database needs, built from dev:
- the skill catalogue and its corrections;
- the dev-added skills, all reviewed (see [the review list](seed_review_dev_added_skills.md)). 94 are seeded; the 13 merged or dropped in dev are not;
- senses, pattern rules and certification mappings;
- scoring v2 and model-capability config, and the default campaign weights (deterministic 40, semantic 30, AI 30).

Never seeded: the per-model daily LLM call cap (set in production, phase 6), AI provider credentials, and embeddings (generated after loading).

### How the seed data is built

`scripts/config_seed/`:
- `export_config.py` exports a database's configuration tables by natural key.
- `diff_config.py` diffs dev against a fresh database built by the first two steps of the fresh-database procedure (phase 6).
- `build_seed_data.py` writes the result to `app/seeds/product_config/`.

`--reviewed <date>` approves the reviewed dev-added skills. `--approve-pending` also approves the skills in the committed file's pending section. A skill added in dev after the committed file was written always waits for review.

The seed is idempotent and never overwrites an edit made after seeding. It doesn't delete anything. `DETERMINISTIC_SCORE_REJECTS` exists in a fresh database but was removed in dev; that's left for review.

Last verified 2026-10-30 on a throwaway local database at migration head `a7c9e1b3d5f6`, with every section approved:
- A fresh database matches dev except the 13 skills merged or dropped in dev, three format-only setting edits (`50.00` to `50`), the call cap and `DETERMINISTIC_SCORE_REJECTS`.
- A second run changes nothing.

## 3. Senior test set - done (2026-10-30)

`tests/fixtures/senior` (S1–S8) and the campaign "[SENIOR] Head of Data & AI Platforms" test a senior, multi-skill JD.

**Fixes it led to:**
- advisory verbs don't count as doing the work;
- "ML" scans as Machine Learning next to a cue word;
- paraphrase skips job-title lines;
- a per-JD requirement text for core skills;
- a new JD version carries the calibration forward as a draft;
- auto-confirm needs the line's leading verb to be a work verb;
- the detection version includes a fingerprint of the rule data, so a score-only re-score rebuilds stale evidence;
- a REVOKED task status;
- a re-score script that refuses double queuing.

**Result:** all 34 synthetic candidates are on target after re-detecting and re-scoring:
- Phase 3: 16/16;
- H1–H10: unchanged;
- senior: S1 and S7 PASS, S2 and S3 HOLD, S4, S5, S6 and S8 REJECT.

Adding descriptions for the senior skills afterwards changed no outcome in a dry run: S4 gains Data Engineering from an ETL line and stays REJECT.

Re-scoring: `python -m scripts.rescore_score_only --campaign "<name pattern>" [--expect N] --check | --go`.

## 4. Phase 4: golden set and checks - done (2026-11-02)

Built in dev on synthetic data. Plan approved 2026-10-30; built in this order:

- [x] **The candidate's name never reaches the votes** (done 2026-10-30). It's masked in every excerpt the confirmation and paraphrase voters see.
- [x] **Golden set** (done 2026-10-30): `tests/golden/`, self-contained and versioned. It holds the 34 synthetic candidates plus counterfactual pairs that differ only in name, career gap or region (23 resumes in 6 groups, each group with a control). See `tests/golden/README.md`.
- [x] **Golden runner** (done 2026-10-30): `python -m scripts.golden.run`.
  - It scores the whole set without saving and compares with the expectations, the counterfactual partners and the accepted run. It writes a diff report with causes and reports LLM calls and tokens.
  - Votes come from a committed cache.
  - The required check is a blocking gate in `scripts/quality_check.sh`, on every merge: the accepted run must match the scoring fingerprint.
  - The first accepted run: all 57 cases as expected, and all 17 counterfactual pairs the same.
- [x] **Recruiter overrides** (done 2026-10-30). A person's move against the system's screening outcome (OVERRIDE), or resolving a HOLD (REVIEW_DECISION), needs a reason code and is recorded with:
  - the result it contradicted;
  - the failed checks and missing core skills;
  - who made the move.

  Reason codes only, no free text. The report is in the monitoring panel.
- [x] **Monitoring view (HR_ADMIN)** (done 2026-10-30): the Scoring monitoring panel on the AIRS dashboard. It shows:
  - outcomes over time;
  - uncertain and split-vote rates;
  - LLM calls, tokens and estimated cost per model, campaign and resume, at editable list prices;
  - quota per model;
  - failed, stuck and PENDING work.
- [x] **Fairness report** (done 2026-10-30), in the same panel. Pass rates by experience band, career gap, education level, region (state or country, never city) and internships, for all candidates and for those meeting the minimum experience. Groups under the minimum size (a setting, 10) are hidden, with notes on what it can't show.

**Follow-ups, done 2026-11-02 unless open:**
- Evaluate the scoring ideas in [scoring_v2_phase4_candidates.md](scoring_v2_phase4_candidates.md) with the golden runner, the set extended with cases that show each problem, before any is built:
  - clause splitting for paraphrase lines;
  - one line confirming two core skills;
  - title lines as weak evidence.
- [x] A stated career break no longer counts as experience (2026-11-02); golden pair GAP-S2.
- [x] The candidate's name is hidden from the resume parse (2026-11-02). With the name visible, a name changed which skills the parse kept; with it hidden, the name variants match ([known issues](known_issues.md)).
- [x] Every email goes through the notifications path (2026-11-02): recorded in `email_notifications` and governed by `EMAIL_DELIVERY`, including the embedding-health HR alert that called SES directly.
- Still open: the three scoring ideas above; refreezing the other 50 golden resumes with the name hidden.
- [x] The older HR override report is retired (2026-11-02). Its rate alert reads the new overrides table (candidates overridden / candidates with a decided outcome).
- [x] The pipeline board's drag-and-drop goes through the stage-move endpoint and the reason-code picker (2026-11-02). That endpoint allows recruiters and hiring managers, so an HR_ADMIN's drag is refused (403) with the server's message.
- Monitoring stays a dashboard panel for now.

## 5. Retire v1 - in progress

Plan approved 2026-11-02, built in order, each phase with a golden run before merge:

- [x] **A. An undecided v2 result never rejects** (2026-11-02). The daily cap (NOT_RUN) no longer rejects; PENDING is retried with backoff, then dead-lettered; an hourly job re-queues candidates whose decision never landed.
- [x] **B. No-LLM fallback** (2026-11-02). The daily cap falls back at once, a provider failure on the last attempt. Scan, auto-confirm and rules only:
  - REJECT only on a rule-only failure (experience beyond the tolerance, missing mandatory education);
  - any core skill that needs a vote, or isn't found, is UNCERTAIN, so HOLD, marked "Awaiting full scoring";
  - every core skill auto-confirmed: the full outcome.

  Fallback HOLDs are re-scored hourly once quota is back. The full outcome is applied only if no person has acted on the candidate since; otherwise it's recorded and the stage is left alone. The golden set has a no-LLM accepted run, checked against each case's `expected_no_llm`.
- [x] **C. v2 decides every campaign** (2026-11-03). v1 no longer runs; the candidate holds v2's own breakdown, and the scorecard, analytics and exports read it. The resume used is the latest parsed version. Migration `d8f0a2c4e6b9` moved the stored breakdowns; the old ones are kept in `campaign_candidate_breakdown_backup` until phase E is done (dropping it needs approval).
  - A re-parse of a rejected candidate is not an override: the corrected input is re-scored and the new score decides. Monitoring counts these re-parses separately, per campaign and per requester.
- [ ] **D.** Shadow mode generalised to "current vs candidate scoring version".
- [ ] **E.** The v1 code, tests and settings removed.

## 6. Go to production

1. **Code review** of the whole system.
2. **Deploy to the dev environment** and re-run all test sets there (the golden runner).
3. **Production on a fresh database,** with migrations and the seed only - never a copy of dev (procedure below).
4. Before production holds real candidate data:
   - [ ] **Gemini billing:** move to the paid tier. See [known issues](known_issues.md).
   - [ ] **Per-model daily LLM call caps:** set `SCORING_V2_DAILY_LLM_CALL_CAP` per model once billing is on, for example `{"default": N, "<model>": N}`. It's never seeded; dev's 400-per-model caps fit the free tier, and a fresh database has the base seed's single default of 300.
   - [ ] **Backup model:** configure a backup model and test that scoring falls back to it.
   - [ ] **Real email tested deliberately:** a planned, explicit test of real delivery. Until then `EMAIL_DELIVERY=log` everywhere.
   - [ ] **One email path only:** every email is an `email_notifications` row sent by `notifications.send_candidate_email`; nothing else calls SES (`tests/tasks/test_one_email_path.py` guards it). Check it still holds before go-live.
   - [ ] **No other deployment shares the production database:** confirm that only the production API and workers connect to it (check `pg_stat_activity` client addresses). A second worker on the dev database ran its own scheduled jobs, including an HR alert email, for weeks unnoticed.
   - [ ] **No personal data in logs:** worker logs, task logs and dead-letter rows. See [known issues](known_issues.md).
   - [ ] **Retention policies:** `retention_policies` is empty. Set retention days per entity type and jurisdiction, as GDPR requires, and seed them.

### Fresh database procedure

1. `alembic upgrade head`
2. The base seeds:
   - `seed_platform_config`, `seed_prompt_templates`, `seed_allowed_transitions`, `seed_email_templates`, `seed_skill_ontology_stopgap`, `seed_scoring_v2`;
   - the encryption-key seeds.

   Don't run the test-data seeds (`seed_m12_test_campaign`, `seed_users`, `import_aiven_hiring_manager`).
3. `python -m app.seeds.seed_product_config --dry-run`, check its report, then run it without `--dry-run`.
4. `python scripts/generate_missing_skill_embeddings.py`. Embeddings are never seeded.
5. Enter the AI provider credentials and model assignments in the app. They're never exported from dev.
6. **Required, after enabling billing:** set `SCORING_V2_DAILY_LLM_CALL_CAP` per model, in the platform settings.

On a fresh database, the migrations that add certification mappings, skill descriptions and the ITSM sense skip their rows, because they run before the skill catalogue exists. Step 3 supplies those rows.
