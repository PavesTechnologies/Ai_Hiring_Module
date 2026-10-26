# Roadmap

## Production checklist

Before production holds real candidate data:

- [x] **Review the dev-added skills before export:** done 2026-10-26. Decisions are in [the review list](seed_review_dev_added_skills.md). They're applied in dev and audited. The re-score of the 26 synthetic candidates showed no change. The seed was regenerated with every section approved: 90 dev-added skills are seeded, and the 13 merged or dropped ones are not.
- [ ] **Review the 4 skills added after that review:** Data Engineering, Data Governance, Stakeholder Management and AWS Certified Solutions Architect, added while calibrating the senior test JD. They're in a separate unapproved section; see [the review list](seed_review_dev_added_skills.md), 'Added after the review'.
- [ ] **Define data retention policies.** `retention_policies` is empty in dev, so nothing is retained or purged by policy. Set retention days per entity type and jurisdiction, as GDPR requires, and seed them.
- [x] **Default campaign weights:** decided 2026-10-26 to use the dev values (deterministic 40, semantic 30, AI 30). They're seeded.
- [ ] **Move to the paid Gemini tier first.** See [known issues](known_issues.md).
- [ ] **Required: set the per-model daily LLM call cap after enabling billing.** `SCORING_V2_DAILY_LLM_CALL_CAP` is never seeded from dev, whose 400-per-model caps fit the free tier. A fresh database has the base seed's single default of 300. Set it per model, for example `{"default": N, "<model>": N}`, once billing is on. See step 6 below.
- [ ] **Keep personal data out of logs.** Worker logs, task logs and dead-letter rows must not contain personal data. See [known issues](known_issues.md).
- [ ] **Score the [P3] fixtures on staging** against the fresh database. Expect Phase 3 16/16 and H1–H10 unchanged.

## Fresh database (production)

Production starts from an empty database, never a copy of dev. In this order:

1. `alembic upgrade head`
2. The base seeds: `seed_platform_config`, `seed_prompt_templates`, `seed_allowed_transitions`, `seed_email_templates`, `seed_skill_ontology_stopgap`, `seed_scoring_v2`. Also the encryption-key seeds. Don't run the test-data seeds (`seed_m12_test_campaign`, `seed_users`, `import_aiven_hiring_manager`).
3. `python -m app.seeds.seed_product_config --dry-run`, check its report, then run it without `--dry-run`.
4. `python scripts/generate_missing_skill_embeddings.py`. Embeddings are never seeded.
5. Enter the AI provider credentials and model assignments in the app. They're never exported from dev.
6. **Required, after enabling billing:** set `SCORING_V2_DAILY_LLM_CALL_CAP` per model, in the platform settings.

On a fresh database, the migrations that add certification mappings, skill descriptions and the ITSM sense skip their rows, because they run before the skill catalogue exists. Step 3 supplies those rows.

### How the seed data is built

`scripts/config_seed/`: `export_config.py` exports the configuration tables of a database by natural key. `diff_config.py` diffs dev against a fresh database built by steps 1–2. `build_seed_data.py` writes the reviewed result to `app/seeds/product_config/`.

The seed is idempotent and never overwrites an edit made after seeding. It doesn't delete anything. `DETERMINISTIC_SCORE_REJECTS` exists in a fresh database but was removed in dev; that's left for review.

Last verified 2026-10-26 on a throwaway local database, with every section approved:
- A fresh database matches dev except the 13 skills merged or dropped in dev (inactive, not seeded by design), three format-only setting edits (`50.00` to `50`), the call cap (set in production) and `DETERMINISTIC_SCORE_REJECTS`.
- A second run changes nothing.
