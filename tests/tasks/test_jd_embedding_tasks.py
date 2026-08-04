from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus

TASKS_MODULE = "app.tasks.jd_embedding_tasks"


def _make_jd(is_active_version=True):
    return SimpleNamespace(id=uuid4(), is_active_version=is_active_version)


def _make_campaign(name="Backend Engineer Hiring"):
    return SimpleNamespace(id=uuid4(), name=name)


class _Harness:
    """Mirrors the _Harness pattern already established in test_embedding_tasks.py."""

    def __init__(self):
        self.jd_repo = MagicMock()
        self.jd_repo.get_linked_campaigns.return_value = []
        self.skill_repo = MagicMock()
        self.config_repo = MagicMock()
        self.audit_service_instance = MagicMock()
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None

        def _create(log):
            log.retry_count = log.retry_count or 0
            return log

        self.task_log_repo.create.side_effect = _create
        self.task_log_repo.update.side_effect = lambda log: log
        self.dead_letter_queue_repo = MagicMock()
        self.embedding_service_instance = MagicMock()
        self.jd_embedding_service_instance = MagicMock()
        self.jd_embedding_service_instance.generate_and_store_embedding.return_value = SimpleNamespace(id=uuid4())

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.SkillRepository", return_value=self.skill_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
            patch(f"{TASKS_MODULE}.EmbeddingService", return_value=self.embedding_service_instance),
            patch(f"{TASKS_MODULE}.JDEmbeddingService", return_value=self.jd_embedding_service_instance),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_skips_duplicate_run_when_task_log_already_success():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(uuid4()))

        h.jd_repo.get_by_id.assert_not_called()
        h.jd_embedding_service_instance.generate_and_store_embedding.assert_not_called()


def test_skips_gracefully_when_jd_no_longer_exists():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        h.jd_repo.get_by_id.return_value = None

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(uuid4()))

        h.jd_embedding_service_instance.generate_and_store_embedding.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_skips_when_jd_not_active_version():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=False)
        h.jd_repo.get_by_id.return_value = jd

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id))

        h.jd_embedding_service_instance.generate_and_store_embedding.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_generates_embedding_and_marks_success_for_active_jd():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id), force_regenerate=False)

        h.jd_embedding_service_instance.generate_and_store_embedding.assert_called_once_with(
            jd.id, force_regenerate=False,
        )
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_does_not_raise_campaign_alerts_when_not_force_regenerate():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd
        h.jd_repo.get_linked_campaigns.return_value = [_make_campaign()]

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id), force_regenerate=False)

        h.audit_service_instance.log.assert_not_called()


def test_force_regenerate_raises_campaign_health_alert_for_every_linked_campaign():
    """
    Requirement 6: once a skill-triggered re-embedding actually completes,
    every campaign linked to this JD gets a CAMPAIGN_HEALTH_ALERT warning
    HR that candidates already scored may need semantic re-scoring.
    """
    from app.enums.constants import ActionType, EntityType
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd
        campaign_a = _make_campaign("Backend Hiring")
        campaign_b = _make_campaign("Frontend Hiring")
        h.jd_repo.get_linked_campaigns.return_value = [campaign_a, campaign_b]

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id), force_regenerate=True)

        assert h.audit_service_instance.log.call_count == 2
        for call in h.audit_service_instance.log.call_args_list:
            assert call.kwargs["action_type"] == ActionType.CAMPAIGN_HEALTH_ALERT
            assert call.kwargs["entity_type"] == EntityType.CAMPAIGN
            assert call.kwargs["details"]["condition"] == "JD_EMBEDDING_UPDATED"
            assert call.kwargs["details"]["jd_id"] == str(jd.id)

        flagged_campaign_ids = {call.kwargs["entity_id"] for call in h.audit_service_instance.log.call_args_list}
        assert flagged_campaign_ids == {campaign_a.id, campaign_b.id}

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_force_regenerate_with_no_linked_campaigns_raises_no_alerts():
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd
        h.jd_repo.get_linked_campaigns.return_value = []

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id), force_regenerate=True)

        h.audit_service_instance.log.assert_not_called()


def test_dead_letters_when_jd_embedding_service_raises_value_error():
    """A ValueError (e.g. JD not found mid-run) classifies PERMANENT -> dead-letter immediately, no retry."""
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd
        h.jd_embedding_service_instance.generate_and_store_embedding.side_effect = ValueError("JD vanished")

        generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "EMBED_JD"
        assert create_kwargs["input_payload"]["jd_id"] == str(jd.id)

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.DEAD


def test_retries_on_transient_failure():
    """A ConnectionError classifies TRANSIENT - must retry, never dead-letter, on the first attempt."""
    from app.tasks.jd_embedding_tasks import generate_jd_embedding

    with _Harness() as h:
        jd = _make_jd(is_active_version=True)
        h.jd_repo.get_by_id.return_value = jd
        h.jd_embedding_service_instance.generate_and_store_embedding.side_effect = ConnectionError(
            "embedding model unreachable",
        )

        with pytest.raises(ConnectionError):
            generate_jd_embedding(task_id=str(uuid4()), jd_id=str(jd.id))

        h.dead_letter_queue_repo.create.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.RETRY
