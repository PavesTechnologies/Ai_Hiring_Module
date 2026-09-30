from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.campaign.campaign_service import CampaignService
from app.services.campaign.dlq_error_summary import summarize_dlq_error
from app.services.celery_task_log_service import CeleryTaskLogService

"""
DLQ chains: a failed replay dead-letters a new row (kept for audit + the
replay limit), a later success resolves the chain, and the UI gets one
short error phrase instead of the raw provider reply.
"""

GEMINI_DAILY = (
    "Gemini API error: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota ... quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier ... limit: 20'}}"
)


# ----------------------------------------------------------------------
# summarize_dlq_error
# ----------------------------------------------------------------------

def test_summary_for_provider_errors():
    assert summarize_dlq_error(GEMINI_DAILY) == "AI provider daily limit reached"
    assert summarize_dlq_error("Gemini API error: 503 UNAVAILABLE. overloaded") == "AI provider was unavailable"
    assert summarize_dlq_error("Anthropic API error: Error code: 401 - invalid x-api-key") == (
        "AI provider API key is invalid"
    )
    assert summarize_dlq_error("Groq connection error: reset by peer") == "AI provider was unavailable"
    assert summarize_dlq_error("Gemini returned invalid JSON: Expecting value") == "AI returned an unusable response"


def test_summary_for_plain_errors_is_first_sentence_truncated():
    assert summarize_dlq_error("Resume 'x' has no parsed_json. Cannot run.") == "Resume 'x' has no parsed_json."
    long = "x" * 300
    assert len(summarize_dlq_error(long)) <= 90
    assert summarize_dlq_error(None) == "Processing failed"
    assert summarize_dlq_error("   ") == "Processing failed"


# ----------------------------------------------------------------------
# mark_success resolves the chain
# ----------------------------------------------------------------------

def _log(**overrides):
    fields = dict(task_id="t-2", task_type="AI_EVALUATE", campaign_candidate_id=uuid4(), resume_id=None,
                  status=None, output_summary=None, completed_at=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_mark_success_resolves_dead_letters_for_candidate_task():
    repository = MagicMock()
    log = _log()
    repository.update.return_value = log

    with patch("app.services.celery_task_log_service.DeadLetterQueueRepository") as dlq_repo_cls:
        CeleryTaskLogService(repository).mark_success(log, summary="ok")

    kwargs = dlq_repo_cls.return_value.resolve_open.call_args.kwargs
    assert kwargs["task_type"] == "AI_EVALUATE"
    assert kwargs["campaign_candidate_id"] == log.campaign_candidate_id
    assert "t-2" in kwargs["resolution_notes"]
    repository.db.begin_nested.assert_called_once()   # savepoint, no commit of its own
    repository.commit.assert_called_once()


def test_mark_success_skips_resolution_for_entity_less_tasks():
    repository = MagicMock()
    log = _log(campaign_candidate_id=None, resume_id=None, task_type="RESUME_UPLOAD_RECOVERY_SCAN")
    repository.update.return_value = log

    with patch("app.services.celery_task_log_service.DeadLetterQueueRepository") as dlq_repo_cls:
        CeleryTaskLogService(repository).mark_success(log, summary="ok")

    dlq_repo_cls.assert_not_called()
    repository.commit.assert_called_once()


def test_resolution_failure_never_breaks_mark_success():
    repository = MagicMock()
    log = _log()
    repository.update.return_value = log

    with patch("app.services.celery_task_log_service.DeadLetterQueueRepository") as dlq_repo_cls:
        dlq_repo_cls.return_value.resolve_open.side_effect = RuntimeError("db hiccup")
        result = CeleryTaskLogService(repository).mark_success(log, summary="ok")

    assert result is log
    repository.commit.assert_called_once()


# ----------------------------------------------------------------------
# Chain view on the campaign DLQ list
# ----------------------------------------------------------------------

def _row(minutes_ago, replayed=False, resolved=False, error=GEMINI_DAILY, cc_id=None):
    moved = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return SimpleNamespace(
        id=uuid4(), task_type="AI_EVALUATE", campaign_candidate_id=cc_id, resume_id=None,
        final_error_message=error, retry_count=1, moved_to_dlq_at=moved, last_attempted_at=moved,
        resolution_notes=None, replayed_at=moved if replayed else None, resolved_at=moved if resolved else None,
    )


def _service(campaign_repo, max_replays="3"):
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"MAX_DLQ_REPLAYS_PER_TASK": max_replays}
    return CampaignService(
        campaign_repo=campaign_repo, jd_repo=MagicMock(), audit_service=MagicMock(), config_repo=config_repo,
        preset_repo=MagicMock(), db=MagicMock(), circuit_breaker_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(), prompt_template_repo=MagicMock(), resume_repo=MagicMock(),
    )


def _list(head, chain, max_replays="3"):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(id=uuid4())
    campaign_repo.get_dlq_chain_heads_page.return_value = ([head], 1)
    campaign_repo.get_dlq_chain_rows.return_value = {head.id: chain}
    campaign_repo.get_candidate_ids_for_dlq_entries.return_value = {}
    with patch("app.services.campaign.campaign_service.CandidateRepository") as candidate_repo_cls, \
            patch("app.services.campaign.campaign_service.EncryptionService"), \
            patch("app.services.campaign.campaign_service.EncryptionKeyRepository"):
        candidate_repo_cls.return_value.get_by_ids.return_value = []
        return _service(campaign_repo, max_replays).get_dead_letter_queue_for_campaign(uuid4())


def test_chain_shows_one_entry_with_history_and_short_error():
    cc_id = uuid4()
    head, older = _row(1, cc_id=cc_id), _row(30, replayed=True, cc_id=cc_id)

    page = _list(head, [head, older])

    entry = page.entries[0]
    assert entry.id == head.id
    assert entry.status == "OPEN"
    assert entry.attempt_count == 2
    assert entry.replays_used == 1
    assert entry.replay_limit == 3
    assert entry.error_summary == "AI provider daily limit reached"
    assert [h.id for h in entry.history] == [older.id]
    assert entry.history[0].error_summary == "AI provider daily limit reached"


def test_chain_status_replaying_and_limit_reached():
    head = _row(1, replayed=True)
    assert _list(head, [head]).entries[0].status == "REPLAYING"

    head = _row(1)
    chain = [head, _row(10, replayed=True), _row(20, replayed=True)]
    assert _list(head, chain, max_replays="2").entries[0].status == "LIMIT_REACHED"
