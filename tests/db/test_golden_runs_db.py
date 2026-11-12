"""Against Postgres (rolled back): golden runs for a scoring version (settings phase 3), with a fake runner."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.scoring_v2 import ScoringVersion, ScoringVersionGoldenRun
from app.services.settings import golden_runs as gr
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture

GOOD = {"cases": 60, "cases_scored": 60, "expectation_failures": [], "pair_failures": [], "cache_misses": [],
        "changed_vs_accepted": ["S1"], "expected_llm_calls": 0, "llm_calls": 6, "stopped": None, "outcomes": {}}


def _version(db, status="candidate"):  # noqa: F811
    version = ScoringVersion(id=uuid4(), label=f"try {uuid4()}", status=status,
                             configuration={"importance_weights": {"core": 3.0}})
    db.add(version)
    db.flush()
    return version


def _start(db, version, kind):  # noqa: F811
    with patch("app.tasks.golden_run_tasks.golden_run_task") as task:
        view = gr.GoldenRunService(db).start(version.id, kind, actor_id=None)
    task.apply_async.assert_called_once()
    return db.get(ScoringVersionGoldenRun, __import__("uuid").UUID(view["id"]))


def _runner(results):
    """A fake scripts.golden.run.run: fills result_holder from results in call order; records the args."""
    calls = []

    def run(args):
        calls.append(args)
        args.result_holder.update(results[len(calls) - 1])
        return 0
    run.calls = calls
    return run


def test_a_run_needs_a_fresh_estimate_of_the_exact_configuration(db):  # noqa: F811
    version = _version(db)
    with pytest.raises(CampaignException) as exc:
        gr.GoldenRunService(db).start(version.id, gr.KIND_RUN, actor_id=None)
    assert exc.value.data["code"] == "ESTIMATE_REQUIRED"


def test_the_estimate_makes_no_llm_call_and_records_the_expected_calls(db):  # noqa: F811
    version = _version(db)
    run = _start(db, version, gr.KIND_ESTIMATE)
    runner = _runner([{**GOOD, "expected_llm_calls": 12, "cache_misses": ["S1"]}])
    gr.execute(db, str(run.id), runner=runner)
    db.refresh(run)
    assert (run.status, run.expected_llm_calls) == ("ESTIMATED", 12)
    assert runner.calls[0].cache_only is True and runner.calls[0].scoring_version_id == str(version.id)


def test_a_run_passes_with_both_modes_clean_and_unlocks_promotion(db):  # noqa: F811
    version = _version(db)
    estimate = _start(db, version, gr.KIND_ESTIMATE)
    gr.execute(db, str(estimate.id), runner=_runner([GOOD]))
    with patch.object(gr, "budget_problem", return_value=None):
        run = _start(db, version, gr.KIND_RUN)
        runner = _runner([GOOD, {**GOOD, "llm_calls": 0}])
        gr.execute(db, str(run.id), runner=runner)
    db.refresh(run)
    assert run.status == "PASSED" and run.llm_calls == 6
    assert [a.no_llm for a in runner.calls] == [False, True] and runner.calls[0].guard is not None
    assert gr.GoldenRunService(db).passing_run(version).id == run.id


def test_an_expectation_failure_or_cache_miss_fails_the_run(db):  # noqa: F811
    version = _version(db)
    gr.execute(db, str(_start(db, version, gr.KIND_ESTIMATE).id), runner=_runner([GOOD]))
    with patch.object(gr, "budget_problem", return_value=None):
        run = _start(db, version, gr.KIND_RUN)
        gr.execute(db, str(run.id), runner=_runner([
            {**GOOD, "expectation_failures": [{"case": "S2", "problems": 1}]}, GOOD]))
    db.refresh(run)
    assert run.status == "FAILED" and gr.GoldenRunService(db).passing_run(version) is None


def test_a_run_that_would_eat_the_reserve_is_refused_and_one_that_reaches_it_stops(db):  # noqa: F811
    version = _version(db)
    gr.execute(db, str(_start(db, version, gr.KIND_ESTIMATE).id), runner=_runner([{**GOOD, "expected_llm_calls": 40}]))
    with patch.object(gr, "budget_problem", return_value=gr.STOP_RESERVE):
        with pytest.raises(CampaignException) as exc:
            gr.GoldenRunService(db).start(version.id, gr.KIND_RUN, actor_id=None)
    assert exc.value.data["code"] == gr.STOP_RESERVE and exc.value.data["budget"]["needed"] == 40
    with patch.object(gr, "budget_problem", return_value=None):
        run = _start(db, version, gr.KIND_RUN)
        gr.execute(db, str(run.id), runner=_runner([{**GOOD, "stopped": gr.STOP_RESERVE, "cases_scored": 12}]))
    db.refresh(run)
    assert (run.status, run.stop_reason) == ("STOPPED", gr.STOP_RESERVE)


def test_the_guard_stops_before_a_case_once_the_reserve_would_be_used(db):  # noqa: F811
    version = _version(db)
    gr.execute(db, str(_start(db, version, gr.KIND_ESTIMATE).id), runner=_runner([GOOD]))
    with patch.object(gr, "budget_problem", return_value=None):
        run = _start(db, version, gr.KIND_RUN)
    seen = []

    def runner(args):
        seen.append(args.guard({"id": "S1"}) if args.guard else None)
        args.result_holder.update({**GOOD, "stopped": seen[0]} if seen[0] else GOOD)
    with patch.object(gr, "budget_problem", return_value=gr.STOP_AWAITING):
        gr.execute(db, str(run.id), runner=runner)
    db.refresh(run)
    assert seen == [gr.STOP_AWAITING] and run.stop_reason == gr.STOP_AWAITING


def test_only_one_run_at_a_time_and_never_for_a_retired_version(db):  # noqa: F811
    version = _version(db)
    _start(db, version, gr.KIND_ESTIMATE)                                    # left QUEUED
    with pytest.raises(CampaignException) as exc:
        gr.GoldenRunService(db).start(version.id, gr.KIND_ESTIMATE, actor_id=None)
    assert exc.value.status_code == 409
    with pytest.raises(CampaignException):
        gr.GoldenRunService(db).start(_version(db, status="retired").id, gr.KIND_ESTIMATE, actor_id=None)


def test_a_configuration_changed_before_the_run_is_stopped(db):  # noqa: F811
    version = _version(db)
    run = _start(db, version, gr.KIND_ESTIMATE)
    version.configuration = {"importance_weights": {"core": 2.5}}
    db.flush()
    runner = _runner([GOOD])
    gr.execute(db, str(run.id), runner=runner)
    db.refresh(run)
    assert (run.status, run.stop_reason) == ("STOPPED", "CONFIGURATION_CHANGED") and runner.calls == []


def test_the_budget_keeps_the_reserve_for_candidates():
    version = ScoringVersion(id=uuid4(), label="x", status="candidate", configuration={}, model="m1")
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 0                         # nobody awaiting full scoring
    with patch("app.services.campaign.scoring_v2_service.daily_llm_call_caps", return_value={"default": 400}), \
            patch("app.services.llm.call_log.calls_in_quota_day", return_value=250), \
            patch.object(gr, "reserved_calls", return_value=100):
        assert gr.budget_problem(db, version, 50) is None                    # 250 + 50 + 100 = 400
        assert gr.budget_problem(db, version, 51) == gr.STOP_RESERVE


# --- 2026-11-12: no email and no dispatch during an in-app run; baselines in the database -----------------------

def test_nothing_can_be_dispatched_or_emailed_during_a_run_and_everything_is_back_after():
    from celery.app.task import Task
    from app.repositories.email_notification_repository import EmailNotificationRepository
    from app.services.notifications.ses_email_client import SESEmailClient
    before = (Task.apply_async, EmailNotificationRepository.create, SESEmailClient.send_email)
    with gr.no_email_and_no_dispatch():
        for call in (lambda: Task.apply_async(MagicMock()), lambda: Task.delay(MagicMock()),
                     lambda: EmailNotificationRepository(MagicMock()).create(MagicMock()),
                     lambda: EmailNotificationRepository(MagicMock()).create_unless_duplicate(MagicMock()),
                     lambda: SESEmailClient.send_email(MagicMock(), to_address="x", subject="s", body_text="b")):
            with pytest.raises(gr.EmailOrDispatchDuringGoldenRun):
                call()
    assert (Task.apply_async, EmailNotificationRepository.create, SESEmailClient.send_email) == before


def test_a_run_that_tries_to_queue_anything_ends_in_error(db):  # noqa: F811
    from celery.app.task import Task
    version = _version(db)
    run = _start(db, version, gr.KIND_ESTIMATE)

    def runner(args):
        Task.apply_async(MagicMock())                       # e.g. an email task
    gr.execute(db, str(run.id), runner=runner)
    db.refresh(run)
    assert (run.status, run.stop_reason) == ("ERROR", "EmailOrDispatchDuringGoldenRun")


def test_promotion_makes_the_passing_run_the_baseline_later_runs_compare_with(db):  # noqa: F811
    from app.services.campaign.scoring_versions import ScoringVersionService, current_version
    version = _version(db)
    gr.execute(db, str(_start(db, version, gr.KIND_ESTIMATE).id), runner=_runner([GOOD]))
    baseline_cases = {"S1": {"outcome": "PASS"}}
    with patch.object(gr, "budget_problem", return_value=None):
        run = _start(db, version, gr.KIND_RUN)
        gr.execute(db, str(run.id), runner=_runner([{**GOOD, "_results": {"cases": baseline_cases}},
                                                     {**GOOD, "_results": {"cases": baseline_cases}}]))
    db.refresh(run)
    assert run.results["llm"]["cases"] == baseline_cases and "_results" not in run.summary["llm"]
    ScoringVersionService(db, MagicMock()).promote(version.id, actor_id=None, actor_role="HR_ADMIN")
    assert current_version(db).golden_baseline_run_id == run.id
    accepted, source = gr.baseline_for_comparison(db, "llm")
    assert accepted == {"cases": baseline_cases} and source == f"baseline:{run.id}"

    other = _version(db)
    estimate = _start(db, other, gr.KIND_ESTIMATE)
    runner = _runner([GOOD])
    gr.execute(db, str(estimate.id), runner=runner)
    assert runner.calls[0].accepted_override == {"cases": baseline_cases}
    db.refresh(estimate)
    assert estimate.summary["estimate"]["compared_with"] == f"baseline:{run.id}"


def test_without_a_baseline_the_repository_run_is_used(db):  # noqa: F811
    from app.services.campaign.scoring_versions import current_version
    current = current_version(db)
    current.golden_baseline_run_id = None
    db.flush()
    assert gr.baseline_for_comparison(db, "llm") == (None, "repository")

