"""
Covers the WebSocket board.candidate_added gap in the bulk-upload flow:
parse_bulk_upload_file's duplicate-file branch (an already-known resume
file re-linked to a new campaign) creates a real CampaignCandidate row via
CampaignCandidateService.create_campaign_candidate, exactly like the
individual-upload flow (ResumeIntakeService.upload_resume) - it must
publish board.candidate_added exactly once, and only after the
transaction that persists the new link has actually committed.

The full "genuinely new resume" branch (further down the same task) shares
the identical capture-return-value -> commit -> publish mechanism verified
here; it is not separately harnessed given the size of its own mocking
surface (AI extraction, PII, the full ResumeProcessingPipeline).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.candidates import FileFormat
from app.services.resume.file_validation_service import FileValidationResult

TASKS_MODULE = "app.tasks.bulk_upload_tasks"


class _Harness:
    """Patches every repository/service constructor the task instantiates."""

    def __init__(self, *, already_linked: bool):
        self.job_id = uuid4()
        self.campaign_id = uuid4()
        self.job_file_id = uuid4()
        self.matched_resume_id = uuid4()
        self.matched_candidate_id = uuid4()

        self.job_file = SimpleNamespace(
            id=self.job_file_id,
            bulk_upload_job_id=self.job_id,
            storage_path="org_None/bulk/some-resume.pdf",
            original_filename="some-resume.pdf",
        )
        self.job = SimpleNamespace(
            id=self.job_id,
            campaign_id=self.campaign_id,
            uploaded_by="uploader-1",
            jurisdiction="US",
            ip_address=None,
            user_agent=None,
            prompt_template_id=uuid4(),
        )
        self.matched_resume = SimpleNamespace(id=self.matched_resume_id, candidate_id=self.matched_candidate_id)
        self.matched_candidate = SimpleNamespace(id=self.matched_candidate_id)

        self.file_repo = MagicMock()
        self.file_repo.get_by_id.return_value = self.job_file
        self.file_repo.try_start_processing.return_value = True

        self.job_repo = MagicMock()
        # First call (in the task body) returns the real job fixture;
        # the second call (inside _maybe_finalize_job) returns None so
        # that helper short-circuits without needing realistic counters -
        # both go through this same mocked method.
        self.job_repo.get_by_id.side_effect = [self.job, None]

        self.candidate_repo = MagicMock()
        self.candidate_repo.get_by_id.return_value = self.matched_candidate

        self.resume_repo = MagicMock()
        self.resume_repo.get_by_file_hash_global.return_value = self.matched_resume

        self.campaign_repo = MagicMock()
        self.campaign_repo.get_by_id.return_value = self.job

        self.campaign_candidate_repo = MagicMock()
        self.campaign_candidate_repo.get_by_campaign_and_candidate.return_value = (
            SimpleNamespace(id=uuid4()) if already_linked else None
        )

        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None

        self.storage_service_instance = MagicMock()
        self.storage_service_instance.download_file.return_value = b"%PDF-1.4 fake"

        self.file_validation_service_instance = MagicMock()
        self.file_validation_service_instance.validate.return_value = FileValidationResult(
            file_format=FileFormat.PDF, size_bytes=13,
        )

        self.campaign_candidate_service_instance = MagicMock()
        self.added_response = SimpleNamespace(
            id=uuid4(),
            candidate_id=self.matched_candidate_id,
            pipeline_stage=SimpleNamespace(value="UPLOADED"),
        )
        self.campaign_candidate_service_instance.create_campaign_candidate.return_value = self.added_response

        self.publish_mock = MagicMock()
        self.call_order = []
        self.job_repo.commit.side_effect = lambda: self.call_order.append("job_repo.commit")
        self.publish_mock.side_effect = lambda *a, **k: self.call_order.append("publish")

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.BulkUploadJobFileRepository", return_value=self.file_repo),
            patch(f"{TASKS_MODULE}.BulkUploadJobRepository", return_value=self.job_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CandidateRepository", return_value=self.candidate_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.SkillRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.EncryptionKeyRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.ConsentRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.PromptTemplateRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.CheckpointRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.StageFailureLogRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.DocumentProcessingRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateService", return_value=self.campaign_candidate_service_instance),
            patch(f"{TASKS_MODULE}.StorageService", return_value=self.storage_service_instance),
            patch(f"{TASKS_MODULE}.FileValidationService", return_value=self.file_validation_service_instance),
            patch(f"{TASKS_MODULE}.publish_board_candidate_added", self.publish_mock),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc_info):
        for p in reversed(self._patches):
            p.stop()


def test_duplicate_file_publishes_candidate_added_once_after_commit():
    """
    A duplicate-file match that links a NEW campaign_candidate must publish
    board.candidate_added exactly once, strictly after job_repo.commit()
    (the transaction that actually persists the new link).
    """
    with _Harness(already_linked=False) as h:
        from app.tasks.bulk_upload_tasks import parse_bulk_upload_file

        parse_bulk_upload_file(task_id=str(uuid4()), bulk_upload_job_file_id=str(h.job_file_id))

        h.campaign_candidate_service_instance.create_campaign_candidate.assert_called_once()
        h.job_repo.commit.assert_called_once()
        h.publish_mock.assert_called_once_with(h.campaign_id, h.added_response)
        assert h.call_order == ["job_repo.commit", "publish"]


def test_duplicate_file_already_linked_does_not_publish():
    """
    A duplicate-file match whose candidate is already on this campaign's
    board makes no board-visible change - no candidate is created and no
    event is published.
    """
    with _Harness(already_linked=True) as h:
        from app.tasks.bulk_upload_tasks import parse_bulk_upload_file

        parse_bulk_upload_file(task_id=str(uuid4()), bulk_upload_job_file_id=str(h.job_file_id))

        h.campaign_candidate_service_instance.create_campaign_candidate.assert_not_called()
        h.publish_mock.assert_not_called()
