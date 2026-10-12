# AIRS isolated e2e stack

A self-contained local copy of the AIRS backend for end-to-end checks that must never touch live
systems. Built for the scoring v2 re-parse / re-score check; reusable for other flows.

## What is isolated, and how

| Live system | In this stack | Enforced by |
|---|---|---|
| RDS database | Postgres 16 + pgvector in Docker (`airs-e2e-postgres`, 127.0.0.1:55432) | `DB_HOST=postgres` in the compose file; `.env` is never read (excluded from the image, never mounted) |
| Redis / Celery | Its own Redis (127.0.0.1:56379) and its own worker | compose network |
| Supabase storage | Local files in the `e2e_storage` volume | `STORAGE_BACKEND=local` - the Supabase client is never created |
| AWS SES email | Nothing is sent - each email is only logged (`EMAIL NOT SENT ...`) | `EMAIL_DELIVERY=log` - the SES client is never created |
| Auth (UMS / JWKS) | Test identities inside the e2e driver process only | placeholders on the reserved `.invalid` domain, which never resolves |
| LLM provider | Deterministic stub LLM in the worker (no network) | `scripts/e2e/worker.py`; set `E2E_LLM=real` + a key in `.env.e2e` to use a real model |

Every e2e script starts with `assert_isolated()` and refuses to run if the database isn't the local
container, email isn't log-only, storage isn't local, or any setting mentions a live host.

## First time

```powershell
pwsh e2e/e2e.ps1 build          # builds image airs:e2e from arm.Dockerfile (~5 min, once)
python -m scripts.e2e.make_env  # writes e2e/.env.e2e: fresh encryption keys, placeholders, no live hosts
```

`e2e/.env.e2e` is git-ignored. It never contains production values: encryption keys are generated
fresh, and nothing is copied from `.env` unless you pass `--llm-key-from` (only the Gemini key/model,
never printed).

## Run the re-parse check

```powershell
pwsh e2e/e2e.ps1 test
```

Starts from an empty database every time: migrations, seeds, the skill catalogue, a copy of the
"AI transformation lead Sep" JD, two test campaigns, test users (`e2e-recruiter`, `e2e-hr-admin`,
`e2e-hiring-manager`), the worker - then `scripts/e2e/run_reparse_e2e.py`, which uses the real API
routes as the test recruiter and checks:

1. fixtures uploaded by the recruiter; candidate V rejected by v1
2. V's resume shared into a second campaign, candidate SHORTLISTED there (precondition)
3. re-parse of a rejected candidate without a reason is refused (422)
4. re-parse with a reason is accepted and the previous parse is kept
5. skills replaced (no duplicates), v2 evidence replaced
6. re-embedded
7. re-scored
8. moved REJECTED -> SCREENING by the recruiter, with the reason
9. the Parse changes panel's data (`GET .../parse-changes`) shows the completed diff
10. the shared-resume candidate is marked "score out of date" and it shows on its scorecard data
11. a recruiter re-score of the SHORTLISTED candidate is score-only (stage unchanged)
12. no email: no SES client created, and every scheduled (beat) job run once - still none

Results print as PASS/FAIL and are saved in the storage volume (`/data/storage/e2e_report.json`).

Other commands: `pwsh e2e/e2e.ps1 up` (start and keep the stack), `logs` (follow the worker),
`down` (stop and **delete** the e2e data).

## Fixtures

`tests/fixtures/e2e/` - exported once, read-only, by `scripts/e2e/export_fixtures.py`:
the JD + its skills, the active skill catalogue (no embeddings - regenerated locally), and two
anonymised resumes (`candidate_v.docx`, `candidate_k.docx`): candidate names, employers,
institutions, contact lines and URLs replaced. When the stored resume file can't be downloaded the
fixture is rebuilt from the stored parse (`candidates.json` records which).

The two `.docx` resumes are **local only** (git-ignored): they still carry real candidates' career
details. On a fresh checkout, export them before running the e2e:

    python -m scripts.e2e.export_fixtures --campaign "<campaign>" --resume-v "<file name part>" --resume-k "<file name part>"

## Notes

- The stub LLM's first parse of a resume returns only part of its competency line and later parses
  return all of it, so a re-parse visibly changes the skills.
- The UI isn't driven here (it needs the real login). Step 9 checks the endpoint the Parse changes
  panel renders from.
- Known product bug found by this stack: an upload with "use existing" for a duplicate file doesn't
  add the candidate (see `docs/known_issues.md`); the driver sets that candidate up directly.
