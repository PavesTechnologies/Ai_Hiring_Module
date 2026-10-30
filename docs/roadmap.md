# Roadmap

The agreed order (2026-10-30):

1. Dev data cleanup - **done**
2. Production configuration seed - **done**
3. Senior test set - **done**
4. Phase 4: golden set and checks - **next**
5. Retire v1
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

## 4. Phase 4: golden set and checks - next

Built in dev on synthetic data. The plan is pending review.

- **Golden set:** the 34 synthetic candidates with their expectations, in one versioned format that is easy to extend.
- **Golden runner:** scores the whole set without saving and compares with the expectations and the last accepted run. It writes a diff report of outcome and core-skill changes, with causes. Cache-backed, with LLM calls reported. Runnable on demand, and a required check before merging any change to scoring rules, prompts, models or detection data.
- **Recruiter overrides:** record every time a recruiter moves a candidate against the system's outcome: stage, system outcome, reason code, who. Report the most common override patterns.
- **Monitoring view (HR_ADMIN):**
  - outcome rates per campaign over time;
  - uncertain and split-vote rates;
  - LLM calls, tokens and cost per resume;
  - quota use per model;
  - PENDING and failed tasks.
- **Fairness report:** pass rates compared across groups the system can see without inferring protected attributes, such as experience bands, career gaps, education type and region. It states clearly what it can and can't show.

The scoring ideas noted in [scoring_v2_phase4_candidates.md](scoring_v2_phase4_candidates.md) are evaluated with the golden runner (the set extended with cases that show each problem) before any is built:
- clause splitting for paraphrase lines;
- one line confirming two core skills;
- title lines as weak evidence.

## 5. Retire v1

- Scoring v2 everywhere, with a no-LLM fallback.
- Shadow mode generalised to "current vs candidate version".
- The v1 code removed.

## 6. Go to production

1. **Code review** of the whole system.
2. **Deploy to the dev environment** and re-run all test sets there (the golden runner).
3. **Production on a fresh database,** with migrations and the seed only - never a copy of dev (procedure below).
4. Before production holds real candidate data:
   - [ ] **Gemini billing:** move to the paid tier. See [known issues](known_issues.md).
   - [ ] **Per-model daily LLM call caps:** set `SCORING_V2_DAILY_LLM_CALL_CAP` per model once billing is on, for example `{"default": N, "<model>": N}`. It's never seeded; dev's 400-per-model caps fit the free tier, and a fresh database has the base seed's single default of 300.
   - [ ] **Backup model:** configure a backup model and test that scoring falls back to it.
   - [ ] **Real email tested deliberately:** a planned, explicit test of real delivery. Until then `EMAIL_DELIVERY=log` everywhere.
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
