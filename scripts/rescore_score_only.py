"""
Score-only re-score of a campaign's candidates through the app's own queue (enqueue_score_only ->
the Celery worker): scores are recomputed, pipeline stages are left as they are.

    python -m scripts.rescore_score_only --campaign "[P3]%" --campaign "[SENIOR]%" --expect 34 --check
    python -m scripts.rescore_score_only --campaign "[P3]%" --campaign "[SENIOR]%" --expect 34 --go

--campaign: a campaign name pattern (SQL LIKE), repeatable. --expect N: refuse unless exactly N
candidates match. --check shows who would be queued or skipped and queues nothing; --go queues.

Never queues a candidate who already has a scoring task queued or running. It looks where a task
actually is - the broker queues, the broker's unacked set, and the worker's active / reserved /
scheduled tasks - not at celery_task_log, which can hold stale rows. If the worker doesn't answer,
nothing is queued (a solo-pool worker busy on a task doesn't answer either: wait for it to finish).
Refuses to run unless EMAIL_DELIVERY=log.
"""
import argparse
import base64
import json
import sys

SCORE_TASK_STATES = ("active", "reserved", "scheduled")


class WorkerSilent(Exception):
    """The worker didn't answer an inspect call: what it holds is unknown, so nothing may be queued."""


def task_of_message(raw) -> tuple[str | None, dict]:
    """(task name, kwargs) of a raw broker message: a queue entry, or an unacked [message, exchange, key] entry."""
    message = json.loads(raw)
    if isinstance(message, list):
        message = message[0]
    headers = message.get("headers") or {}
    try:
        body = json.loads(base64.b64decode(message["body"]))
        kwargs = body[1] if isinstance(body, list) and len(body) > 1 else {}
    except Exception:
        kwargs = {}
    return headers.get("task"), kwargs if isinstance(kwargs, dict) else {}


def in_flight_candidate_ids(broker_messages, worker_answers: dict, task_name: str) -> set[str]:
    """
    campaign_candidate_ids with a `task_name` task in the broker (raw messages) or on the worker
    (worker_answers: {"active" | "reserved" | "scheduled": inspect answer, None when it didn't answer}).
    """
    ids = set()
    for raw in broker_messages:
        name, kwargs = task_of_message(raw)
        if name == task_name and kwargs.get("campaign_candidate_id"):
            ids.add(str(kwargs["campaign_candidate_id"]))
    for state in SCORE_TASK_STATES:
        answer = worker_answers.get(state)
        if answer is None:
            raise WorkerSilent(f"The worker didn't answer ({state}) - refusing to queue anything.")
        for tasks in answer.values():
            for task in tasks:
                task = task.get("request", task)            # scheduled entries wrap the request
                kwargs = task.get("kwargs") or {}
                if task.get("name") == task_name and kwargs.get("campaign_candidate_id"):
                    ids.add(str(kwargs["campaign_candidate_id"]))
    return ids


def _broker_messages(celery_app) -> list:
    with celery_app.connection_for_read() as connection:
        client = connection.default_channel.client
        raws = []
        for queue in celery_app.conf.task_queues:
            raws += client.lrange(queue.name, 0, -1)
        raws += list(client.hvals("unacked"))
    return raws


def _worker_answers(celery_app) -> dict:
    inspect = celery_app.control.inspect(timeout=5)
    return {state: getattr(inspect, state)() for state in SCORE_TASK_STATES}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", action="append", required=True, help="campaign name LIKE pattern (repeatable)")
    parser.add_argument("--expect", type=int, help="refuse unless exactly this many candidates match")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--go", action="store_true")
    args = parser.parse_args(argv)

    import logging
    logging.disable(logging.WARNING)
    from app.core.config import settings
    if settings.email_delivery != "log":
        print("EMAIL_DELIVERY must be log - refusing to run.")
        return 2
    from sqlalchemy import or_, select
    import app.models  # noqa: F401
    from app.core.celery_app import celery_app
    from app.db.session import SessionLocal, engine
    from app.models.campaigns import HiringCampaign
    from app.models.candidates import ParseStatus, Resume
    from app.models.pipeline import CampaignCandidate
    from app.services.resume.resume_reparse_service import enqueue_score_only
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    engine.echo = False
    db = SessionLocal()
    try:
        rows = db.execute(select(CampaignCandidate, Resume).join(Resume, Resume.id == CampaignCandidate.resume_id)
                          .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
                          .where(or_(*[HiringCampaign.name.like(p) for p in args.campaign]))).all()
        if args.expect is not None and len(rows) != args.expect:
            print(f"Expected {args.expect} candidates, found {len(rows)} - nothing queued.")
            return 2
        try:
            busy = in_flight_candidate_ids(_broker_messages(celery_app), _worker_answers(celery_app),
                                           calculate_deterministic_score_task.name)
        except WorkerSilent as error:
            print(error)
            return 2
        queued = skipped = 0
        for cc, resume in rows:
            if resume.parse_status != ParseStatus.PARSED:
                print("SKIPPED (not parsed)", resume.original_filename)
                continue
            if str(cc.id) in busy:
                print("SKIPPED (a re-score is already queued or running)", resume.original_filename[:40])
                skipped += 1
                continue
            busy.add(str(cc.id))                                # never twice in one run either
            if args.go:
                print("QUEUED", resume.original_filename[:40], "->", enqueue_score_only(db, cc))
                db.commit()
            else:
                print("WOULD QUEUE", resume.original_filename[:40])
            queued += 1
        print(f"{'queued' if args.go else 'would queue'} {queued}, skipped {skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
