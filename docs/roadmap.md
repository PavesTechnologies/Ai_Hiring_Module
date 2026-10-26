# Roadmap

## Production checklist

Before production holds real candidate data:

- [ ] **Review the dev-added skills before export.** 104 skills were added in dev, 102 by hand and 2 by JD calibration. They sit in `app/seeds/product_config/catalogue_dev_added.json`, which is not loaded until reviewed. Drop any client-specific or personal term, then set `approved: true` on its sections. The 2 skill senses, 2 pattern rules and 11 certification mappings in the same file wait for it, because each names one of those skills.
- [ ] **Define data retention policies.** `retention_policies` is empty in dev, so nothing is retained or purged by policy. Set retention days per entity type and jurisdiction, as GDPR requires, and seed them.
- [ ] **Decide the production values of 3 settings changed in dev.** They're in `app/seeds/product_config/platform_pending_decision.json`, unapproved:
  - the default campaign weights: dev uses deterministic 40 / semantic 30, the seeded values are 30 / 40;
  - `SCORING_V2_DAILY_LLM_CALL_CAP`: dev has free-tier caps of 400 per model.
- [ ] **Move to the paid Gemini tier first.** See [known issues](known_issues.md).
- [ ] **Keep personal data out of logs.** Worker logs, task logs and dead-letter rows must not contain personal data. See [known issues](known_issues.md).
- [ ] **Score the [P3] fixtures on staging** against the fresh database. Expect Phase 3 16/16 and H1–H10 unchanged.

## Fresh database (production)

Production starts from an empty database, never a copy of dev. In this order:

1. `alembic upgrade head`
2. The base seeds: `seed_platform_config`, `seed_prompt_templates`, `seed_allowed_transitions`, `seed_email_templates`, `seed_skill_ontology_stopgap`, `seed_scoring_v2`. Also the encryption-key seeds. Don't run the test-data seeds (`seed_m12_test_campaign`, `seed_users`, `import_aiven_hiring_manager`).
3. `python -m app.seeds.seed_product_config --dry-run`, check its report, then run it without `--dry-run`.
4. `python scripts/generate_missing_skill_embeddings.py`. Embeddings are never seeded.
5. Enter the AI provider credentials and model assignments in the app. They're never exported from dev.

On a fresh database, the migrations that add certification mappings, skill descriptions and the ITSM sense skip their rows, because they run before the skill catalogue exists. Step 3 supplies those rows.

### How the seed data is built

`scripts/config_seed/`: `export_config.py` exports the configuration tables of a database by natural key. `diff_config.py` diffs dev against a fresh database built by steps 1–2. `build_seed_data.py` writes the reviewed result to `app/seeds/product_config/`.

The seed is idempotent and never overwrites an edit made after seeding. It doesn't delete anything. `DETERMINISTIC_SCORE_REJECTS` exists in a fresh database but was removed in dev; that's left for review.

Last verified 2026-10-26 on a throwaway local database:
- With the approved sections, a fresh database matches dev except the pending items.
- With every section approved, it matches dev except three format-only setting edits (`50.00` to `50`) and `DETERMINISTIC_SCORE_REJECTS`.
- A second run changes nothing.
