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


def test_email_delivery_defaults_to_log():
    from app.core.config import Settings
    assert Settings.model_fields["email_delivery"].default == "log"


@pytest.mark.parametrize("value, mode", [("log", "log"), (" LOG ", "log"), ("", "log"), (None, "log"), ("Ses", "ses")])
def test_email_delivery_is_trimmed_and_lower_cased(value, mode):
    from app.core.config import Settings
    assert Settings(email_delivery=value, ses_from_email="hr@example.invalid").email_delivery == mode


@pytest.mark.parametrize("value", ["logs", "LOG_ONLY", "smtp", "true"])
def test_a_misspelt_email_delivery_is_refused_at_startup(value):
    from pydantic import ValidationError
    from app.core.config import Settings
    with pytest.raises(ValidationError, match="EMAIL_DELIVERY must be 'log' or 'ses'"):
        Settings(email_delivery=value)


def test_ses_delivery_without_a_sender_is_refused_at_startup():
    from pydantic import ValidationError
    from app.core.config import Settings
    with pytest.raises(ValidationError, match="needs SES_FROM_EMAIL"):
        Settings(email_delivery="ses", ses_from_email=" ")


def _client_settings(**values):
    from types import SimpleNamespace
    base = dict(email_delivery="log", ses_from_email="hr@example.invalid",
                aws_access_key_id="", aws_secret_access_key="", aws_region="ap-south-1")
    return patch("app.services.notifications.ses_email_client.settings", SimpleNamespace(**{**base, **values}))


@pytest.mark.parametrize("value", ["", "logs", "SES ", None])
def test_only_an_exact_ses_value_counts_as_sending(value):
    from app.services.notifications.ses_email_client import email_delivery_is_log
    with _client_settings(email_delivery=value):
        assert email_delivery_is_log() is (value != "SES ")


def test_the_ses_client_is_refused_outside_ses_mode():
    from app.exceptions.email_exception import EmailDeliveryException
    from app.services.notifications.ses_email_client import SESEmailClient
    with _client_settings(email_delivery="log"), patch("boto3.client", side_effect=AssertionError("SES client created")):
        with pytest.raises(EmailDeliveryException) as raised:
            SESEmailClient()._get_client()
    assert isinstance(raised.value.original, ValueError)


def test_the_ses_client_is_refused_without_a_sender():
    from app.exceptions.email_exception import EmailDeliveryException
    from app.services.notifications.ses_email_client import SESEmailClient
    with _client_settings(email_delivery="ses", ses_from_email=""),             patch("boto3.client", side_effect=AssertionError("SES client created")):
        with pytest.raises(EmailDeliveryException):
            SESEmailClient()._get_client()
