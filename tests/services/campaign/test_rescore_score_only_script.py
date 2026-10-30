"""scripts/rescore_score_only.py: never queues a candidate whose scoring task is already in the broker or on the worker."""
import base64
import json

import pytest

from scripts.rescore_score_only import WorkerSilent, in_flight_candidate_ids, main, task_of_message

TASK = "app.tasks.deterministic_scoring_tasks.calculate_deterministic_score_task"
IDLE = {"active": {"w": []}, "reserved": {"w": []}, "scheduled": {"w": []}}


def _message(cc_id, task=TASK):
    body = base64.b64encode(json.dumps([[], {"campaign_candidate_id": cc_id, "score_only": True}, {}]).encode()).decode()
    return json.dumps({"body": body, "headers": {"task": task, "id": "t-" + cc_id}})


def test_a_queue_message_and_an_unacked_entry_are_read():
    assert task_of_message(_message("cc1")) == (TASK, {"campaign_candidate_id": "cc1", "score_only": True})
    unacked = json.dumps([json.loads(_message("cc2")), "", "celery"])
    assert task_of_message(unacked)[1]["campaign_candidate_id"] == "cc2"


def test_queued_unacked_and_worker_held_scoring_tasks_count_other_tasks_do_not():
    answers = {"active": {"w": [{"name": TASK, "kwargs": {"campaign_candidate_id": "running"}}]},
               "reserved": {"w": [{"name": TASK, "kwargs": {"campaign_candidate_id": "reserved"}}]},
               "scheduled": {"w": [{"request": {"name": TASK, "kwargs": {"campaign_candidate_id": "retry"}}}]}}
    broker = [_message("queued"), json.dumps([json.loads(_message("unacked")), "", "celery"]),
              _message("other", task="app.tasks.semantic.semantic_score_task")]
    assert in_flight_candidate_ids(broker, answers, TASK) == {"queued", "unacked", "running", "reserved", "retry"}


def test_an_idle_worker_and_empty_queues_block_nobody():
    assert in_flight_candidate_ids([], IDLE, TASK) == set()


@pytest.mark.parametrize("state", ["active", "reserved", "scheduled"])
def test_a_silent_worker_blocks_everything(state):
    with pytest.raises(WorkerSilent):
        in_flight_candidate_ids([], {**IDLE, state: None}, TASK)


def test_it_needs_a_campaign_and_a_mode():
    with pytest.raises(SystemExit):
        main(["--check"])                          # no --campaign
    with pytest.raises(SystemExit):
        main(["--campaign", "[P3]%"])              # neither --check nor --go
