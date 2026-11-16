# Contributing

## Merging a branch into RMe

Nothing merges until every check below passes. Merges are fast-forwards, made only when the merge is
asked for, and never pushed as part of the routine. No email may leave this machine: keep
`EMAIL_DELIVERY=log` in `.env`, or `smtp` to a local Mailpit (below). Never `ses` outside production.

### Local email: Mailpit

To see the emails the app would send, run Mailpit and point the app at it. Mailpit catches every
message and relays nothing.

```
docker compose --profile mailpit up -d mailpit        # SMTP 127.0.0.1:1025, web UI http://localhost:8025
```

Then set `EMAIL_DELIVERY=smtp`, `SMTP_HOST=localhost` (or `mailpit` for the compose services) and
`SMTP_PORT=1025`. The app refuses an SMTP host that isn't local (`localhost`, `127.0.0.1`, `::1`,
`mailpit`), and with `APP_ENV=production` it refuses everything except `ses`.

### Working in a git worktree: the `.env` file

`.env` is git-ignored, so a new worktree has none, and the settings won't load without it: golden and
the full suite fail with "Field required" errors. Point the worktree at the main checkout's `.env`
with `AIRS_ENV_FILE` rather than copying it. The settings, the encryption-key loaders and the test
setup all read the file it names (`app/core/env_file.py`). The real environment still comes first, and
a path that doesn't exist is refused rather than skipped.

```
# Git Bash
export AIRS_ENV_FILE=C:/Ai_Hiring_Module/.env
# PowerShell
$env:AIRS_ENV_FILE = "C:\Ai_Hiring_Module\.env"
```

Set it in the shell that runs the checks, for every command run from the worktree.

If a copy is unavoidable (for a tool that only reads `./.env`):

- never commit it: `.env` is in `.gitignore`, so never force-add it;
- delete it yourself when you remove the worktree, and check it's gone. `git worktree remove` deletes
  ignored files too, but a worktree folder deleted by hand, or left behind, keeps the copy: a secret on
  disk.

The e2e stack never reads `.env`. It uses `e2e/.env.e2e`, made by `scripts/e2e/make_env.py`.

### 1. Golden set

```
python -m scripts.golden.run            # LLM only on a vote-cache miss
python -m scripts.golden.run --no-llm
```

The golden runner refuses to start unless `EMAIL_DELIVERY` is `log`. If `.env` uses `smtp` (Mailpit), set
it for the command only, without editing `.env`: `EMAIL_DELIVERY=log python -m scripts.golden.run`.

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

The stack is isolated: its own Postgres, Redis and worker, a stub LLM, and its own Mailpit that
catches every email (`e2e/README.md`). Each driver prints a `[PASS]` or `[FAIL]` line per check and
ends with an `n/n ... checks passed` line. The run then counts the emails Mailpit received from the
drivers that send none, and it fails if that count isn't 0. A failing check blocks the merge like a failing test.

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
  emails Mailpit received: 0
  mailpit            7/7  checks passed
  ```

- **Migrations:** any applied, with their ids.

After the fast-forward, run the full suite again on RMe and add its result to the report.
