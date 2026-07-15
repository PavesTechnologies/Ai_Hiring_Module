# "remaining connection slots are reserved for roles with the SUPERUSER attribute"

If you hit this error, the shared Aiven Postgres instance has run out of
connections. It's a server-side cap, not a bug in your code — but a few
local habits make it much worse. Check these before assuming the instance
itself needs a bigger plan.

## Quick checklist

1. **Close DB GUI clients when you're not actively using them.** Tools
   like DBeaver open a separate connection per SQL editor tab plus one for
   the schema browser — a single idle DBeaver session can hold 5+
   connections against a cap that may only allow a handful total.

2. **Never start the Celery worker with the bare `celery worker` command
   on Windows.** Its default pool is `prefork`, which is not supported on
   Windows — `billiard`'s semaphore emulation breaks
   (`PermissionError: [WinError 5] Access is denied`), workers die and get
   silently replaced, and this can spiral into dozens of zombie processes,
   each capable of opening its own DB connection pool. Always launch it
   with an explicit pool:

   ```
   python -m celery -A app.core.celery_app:celery_app worker --loglevel=info --pool=threads --concurrency=4
   ```

   (`--pool=solo` also works if you don't need concurrent task execution.)

3. **Only run one worker at a time.** Starting a second `celery worker`
   without stopping the first doubles your connection footprint and
   produces a `DuplicateNodenameWarning` (both workers share the same
   default node name on one machine) since Celery can't tell them apart
   for monitoring/control commands. Check before starting a new one:

   ```
   python -m celery -A app.core.celery_app inspect ping
   ```

   If a node already replies, don't start another — resume/reuse it.

4. **Each dev process (FastAPI app + Celery worker) now caps at
   `db_pool_size + db_max_overflow` connections** (see `app/core/config.py`
   / `app/db/database.py`), lowered specifically because this shared
   instance's connection budget is small. Don't raise these values locally
   "to fix" a connection error — that treats the symptom and makes the
   underlying scarcity worse for everyone else on the same instance.

## If it's still happening after all of the above

That means the remaining connections are coming from outside your own
machine — another teammate's session, a staging/CI job, or the Aiven
plan's `max_connections` genuinely being too low for the team's combined
usage. That needs whoever has Aiven console/billing access to either:

- Check current connections / `max_connections` on the Aiven dashboard.
- Attach a PgBouncer pooler in front of this Postgres instance (Aiven
  offers this as an add-on) so many app-side connections share a much
  smaller number of real Postgres backend connections.
- Upgrade the plan tier if `max_connections` is simply too low.
- Consider having each developer run their own local Postgres for
  day-to-day work, reserving the shared instance for staging/CI/demo.

None of these are something an individual developer can fix from their
own machine.
