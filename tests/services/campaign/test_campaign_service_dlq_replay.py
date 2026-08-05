from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.campaign.campaign_service import CampaignService

SERVICE_MODULE = "app.services.campaign.campaign_service"


def _make_dlq_entry(
    task_type="EMBED_RESUME", resume_id=None, campaign_candidate_id=None,
    replayed_at=None, final_error_message="boom",
):
    return SimpleNamespace(
        id=uuid4(), task_type=task_type, resume_id=resume_id, campaign_candidate_id=campaign_candidate_id,
        replayed_at=replayed_at, original_task_id=str(uuid4()), final_error_message=final_error_message,
    )


def _make_service(resume_repo=None, dead_letter_queue_repo=None, campaign_repo=None, config_repo=None):
    campaign_repo = campaign_repo or MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(id=uuid4())
    campaign_repo.count_dlq_chain.return_value = 1

    config_repo = config_repo or MagicMock()
    config_repo.get_configs_by_keys.return_value = {}

    return CampaignService(
        campaign_repo=campaign_repo,
        jd_repo=MagicMock(),
        audit_service=MagicMock(),
        config_repo=config_repo,
        preset_repo=MagicMock(),
        db=MagicMock(),
        circuit_breaker_repo=MagicMock(),
        dead_letter_queue_repo=dead_letter_queue_repo or MagicMock(),
        prompt_template_repo=MagicMock(),
        resume_repo=resume_repo or MagicMock(),
    ), campaign_repo


def _resume(parsed_json=None):
    return SimpleNamespace(id=uuid4(), parsed_json=parsed_json if parsed_json is not None else {"skills": ["Java"]})


# ----------------------------------------------------------------------
# EMBED_RESUME is now a registered, replayable task type.
# ----------------------------------------------------------------------

def test_embed_resume_dlq_entry_replays_when_eligible():
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = _resume()
    resume_repo.get_embedding.return_value = None  # no embedding yet - eligible

    service, campaign_repo = _make_service(resume_repo=resume_repo)
    entry = _make_dlq_entry(resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.replayed_count == 1
    assert response.skipped_count == 0
    assert response.results[0].status == "REPLAYED"
    mock_task.apply_async.assert_called_once()
    call_kwargs = mock_task.apply_async.call_args.kwargs
    assert call_kwargs["kwargs"] == {"resume_id": str(resume_id)}


def test_embed_resume_dlq_entry_marks_replayed_and_logs_audit():
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = _resume()
    resume_repo.get_embedding.return_value = None

    dead_letter_queue_repo = MagicMock()
    service, campaign_repo = _make_service(resume_repo=resume_repo, dead_letter_queue_repo=dead_letter_queue_repo)
    entry = _make_dlq_entry(resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task"):
        service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    dead_letter_queue_repo.mark_replayed.assert_called_once()
    assert dead_letter_queue_repo.mark_replayed.call_args.args[0] == entry.id
    service.audit_service.log.assert_called_once()
    audit_kwargs = service.audit_service.log.call_args.kwargs
    assert audit_kwargs["action_type"].value == "DLQ_TASK_REPLAYED"
    assert audit_kwargs["details"]["task_type"] == "EMBED_RESUME"


def test_embed_resume_dlq_entry_skipped_when_resume_no_longer_exists():
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = None

    service, campaign_repo = _make_service(resume_repo=resume_repo)
    entry = _make_dlq_entry(resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.replayed_count == 0
    assert response.results[0].status == "SKIPPED"
    assert "no longer exists" in response.results[0].reason
    mock_task.apply_async.assert_not_called()


def test_embed_resume_dlq_entry_skipped_when_no_parsed_json():
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = SimpleNamespace(id=resume_id, parsed_json=None)

    service, campaign_repo = _make_service(resume_repo=resume_repo)
    entry = _make_dlq_entry(resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.results[0].status == "SKIPPED"
    assert "parsed_json" in response.results[0].reason
    mock_task.apply_async.assert_not_called()


def test_embed_resume_dlq_entry_skipped_when_embedding_already_exists():
    """The resume was already re-embedded through a different path since this entry died."""
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = _resume()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())

    service, campaign_repo = _make_service(resume_repo=resume_repo)
    entry = _make_dlq_entry(resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.results[0].status == "SKIPPED"
    assert "already exists" in response.results[0].reason
    mock_task.apply_async.assert_not_called()


def test_embed_resume_dlq_entry_not_double_replayed():
    """Already-replayed entries are skipped before the EMBED_RESUME pre-check even runs."""
    resume_id = uuid4()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = _resume()
    resume_repo.get_embedding.return_value = None

    service, campaign_repo = _make_service(resume_repo=resume_repo)
    from datetime import datetime, timezone
    entry = _make_dlq_entry(resume_id=resume_id, replayed_at=datetime.now(timezone.utc))
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.generate_resume_embedding_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.results[0].status == "SKIPPED"
    assert response.results[0].reason == "Already replayed."
    mock_task.apply_async.assert_not_called()
    resume_repo.get_by_id.assert_not_called()


# ----------------------------------------------------------------------
# Baseline regression coverage for the two pre-existing task types - no
# test file covered this engine before, so these establish it wasn't
# broken by adding EMBED_RESUME support.
# ----------------------------------------------------------------------

def test_resume_document_processing_dlq_entry_still_replays_unaffected():
    resume_id = uuid4()
    service, campaign_repo = _make_service()
    entry = _make_dlq_entry(task_type="RESUME_DOCUMENT_PROCESSING", resume_id=resume_id)
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.process_resume_document") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.replayed_count == 1
    mock_task.apply_async.assert_called_once()
    assert mock_task.apply_async.call_args.kwargs["kwargs"] == {"resume_id": str(resume_id)}


def test_deterministic_score_dlq_entry_still_replays_unaffected():
    campaign_candidate_id = uuid4()
    service, campaign_repo = _make_service()
    entry = _make_dlq_entry(
        task_type="DETERMINISTIC_SCORE", campaign_candidate_id=campaign_candidate_id,
    )
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    with patch(f"{SERVICE_MODULE}.calculate_deterministic_score_task") as mock_task:
        response = service.replay_dead_letter_tasks(
            campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
        )

    assert response.replayed_count == 1
    mock_task.apply_async.assert_called_once()
    assert mock_task.apply_async.call_args.kwargs["kwargs"] == {"campaign_candidate_id": str(campaign_candidate_id)}


def test_unsupported_task_type_still_skipped_unaffected():
    service, campaign_repo = _make_service()
    entry = _make_dlq_entry(task_type="SOME_OTHER_TASK_TYPE", resume_id=uuid4())
    campaign_repo.get_dlq_entries_by_ids.return_value = [entry]

    response = service.replay_dead_letter_tasks(
        campaign_id=uuid4(), dlq_ids=[entry.id], replayed_by="hr-1", actor_role="HR_ADMIN",
    )

    assert response.results[0].status == "SKIPPED"
    assert "not supported" in response.results[0].reason
