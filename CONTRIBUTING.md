# Contributing

## Merging a branch into RMe

Nothing merges until every check below passes. Merges are fast-forwards, made only when the merge is
asked for, and never pushed as part of the routine. No check may send email: keep `EMAIL_DELIVERY=log`
in `.env`.

### 1. Golden set

```
python -m scripts.golden.run            # LLM only on a vote-cache miss
python -m scripts.golden.run --no-llm
```

Review each report in `tests/golden/runs/`. If scoring code, a prompt, seed data or the golden set
changed, the fingerprint gate (`tests/golden`) fails until a reviewed run is accepted in the same
change:

```
python -m scripts.golden.run --accept-run tests/golden/runs/<stamp>.json
```

See `tests/golden/README.md`.

### 2. Full suite

```
python -m pytest -q
```

It must pass with no failures; the golden gate is part of it.

### 3. End-to-end, for branches that touch the scoring and task paths

Run this step when the branch changes anything under:

- `app/tasks/`
- `app/services/` (including `app/services/settings/`)
- `alembic/versions/` (migrations)
- settings: `app/api/routes/settings_routes.py`, `app/core/config.py`

Run it from the branch's own tree:

```
pwsh e2e/e2e.ps1 test
```

From Git Bash:

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File e2e/e2e.ps1 test
```

The stack is isolated: its own Postgres, Redis and worker, a stub LLM, and email that is only ever
logged (`e2e/README.md`). Each driver prints a `[PASS]` or `[FAIL]` line per check and ends with an
`n/n ... checks passed` line. The run then counts the emails the worker would have sent, and it fails
if that count isn't 0. A failing check blocks the merge like a failing test.

In a git worktree:

- **Fixtures:** the e2e fixtures `tests/fixtures/e2e/candidate_v.docx` and `candidate_k.docx` are
  git-ignored. Copy them from the main checkout first and delete them afterwards.
- **Removing the worktree:** the stack's containers bind-mount that tree's code. Stop the stack first
  with `docker compose -f <worktree>/e2e/docker-compose.e2e.yml stop`, then remove the worktree.
  `stop` keeps the e2e data; `down` deletes it.

### 4. The merge report

Each merge is reported with:

- **Commits:** the branch's commits and the commit RMe moves to.
- **Golden:** cases, changes against the accepted run, failures and LLM calls, for the full and the
  no-LLM run, plus whether a run was accepted.
- **Full suite:** the pytest summary line, e.g. `4086 passed`.
- **E2E:** either "not needed" with the reason (no path above touched), or each driver's
  `n/n checks passed` line:

  ```
  reparse           12/12 checks passed
  phase 2            7/7  checks passed
  fallback           8/8  checks passed
  scoring versions   6/6  checks passed
  golden estimate    4/4  checks passed
  emails the worker would have sent: 0
  ```

- **Migrations:** any applied, with their ids.

After the fast-forward, run the full suite again on RMe and add its result to the report.
