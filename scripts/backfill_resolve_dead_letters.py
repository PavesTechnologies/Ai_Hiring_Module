"""
One-off backfill: resolve dead_letter_queue rows whose task later succeeded.

Before CeleryTaskLogService.mark_success started resolving DLQ chains, a
successful replay left its DLQ row unresolved forever (it now shows as
"Replay in progress"). This marks every unresolved row resolved when a
celery_task_log SUCCESS exists for the same task_type + entity (same keying
as CampaignRepository.count_dlq_chain) completed after the row was
dead-lettered. Rows are never deleted.

Idempotent. Dry run by default:
    python -m scripts.backfill_resolve_dead_letters           # show what would change
    python -m scripts.backfill_resolve_dead_letters --apply   # write it
"""
import argparse

from sqlalchemy import text

from app.db.session import SessionLocal

_MATCHES = """
    FROM dead_letter_queue d
    JOIN LATERAL (
        SELECT l.task_id, l.completed_at
        FROM celery_task_log l
        WHERE l.task_type = d.task_type
          AND l.status = 'SUCCESS'
          AND l.completed_at > d.moved_to_dlq_at
          AND (
                (d.campaign_candidate_id IS NOT NULL AND l.campaign_candidate_id = d.campaign_candidate_id)
             OR (d.campaign_candidate_id IS NULL AND d.resume_id IS NOT NULL AND l.resume_id = d.resume_id)
          )
        ORDER BY l.completed_at
        LIMIT 1
    ) s ON TRUE
    WHERE d.resolved_at IS NULL
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.execute(text(f"SELECT d.id, d.task_type, s.task_id, s.completed_at {_MATCHES}")).all()
        for row in rows:
            print(f"{row.id}  {row.task_type:<28} resolved by task {row.task_id} at {row.completed_at}")
        print(f"{len(rows)} dead-letter row(s) to resolve.")

        if not args.apply:
            print("Dry run - nothing written. Re-run with --apply to resolve them.")
            return

        updated = db.execute(text(f"""
            UPDATE dead_letter_queue AS t
            SET resolved_at = m.completed_at,
                resolution_notes = 'Resolved: re-run succeeded (task ' || m.task_id || ').'
            FROM (SELECT d.id, s.task_id, s.completed_at {_MATCHES}) AS m
            WHERE t.id = m.id
        """)).rowcount
        db.commit()
        print(f"Resolved {updated} row(s).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
