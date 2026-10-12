"""Isolation switches for local stacks: STORAGE_BACKEND=local (no Supabase) and EMAIL_DELIVERY=log (no SES)."""
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.storage_service import StorageService
from app.exceptions.storage_exception import StorageException


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))
    return tmp_path


def test_local_storage_round_trip_never_touches_supabase(local_storage):
    with patch("app.core.storage_service.get_supabase", side_effect=AssertionError("Supabase used")):
        service = StorageService()
        service.upload_file("resumes", "c1/r.docx", b"hello")
        assert service.file_exists("resumes", "c1/r.docx")
        assert service.download_file("resumes", "c1/r.docx") == b"hello"
        assert service.generate_signed_url("resumes", "c1/r.docx").startswith("file:")
        service.delete_file("resumes", "c1/r.docx")
        assert not service.file_exists("resumes", "c1/r.docx")


def test_local_storage_refuses_paths_outside_its_folder(local_storage):
    with pytest.raises(StorageException):
        StorageService().upload_file("resumes", "../../escape.txt", b"x")


def test_log_delivery_never_creates_an_ses_client(monkeypatch):
    from app.services.notifications.ses_email_client import SESEmailClient
    monkeypatch.setattr(settings, "email_delivery", "log")
    with patch("boto3.client", side_effect=AssertionError("SES client created")):
        assert SESEmailClient().send_email(to_address="a@example.invalid", subject="s", body_text="b").startswith("log-only-")
