"""
Review item 28: no personal data in logs, task logs, dead letters, stage rows or storage keys.

Error sinks get the exception's class (and provider error code), never its message: SES echoes the
recipient, pydantic quotes the model's extraction of a resume, storage errors name paths, database
errors carry row values. Bulk files are staged under their position only. No email.
"""
import ast
import logging
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

from app.core.safe_errors import SafeRetryError, safe_error
from app.exceptions.email_exception import EmailDeliveryException

EMAIL = "jane.example@example.invalid"
APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def _ses_error():
    return ClientError({"Error": {"Code": "MessageRejected", "Message": f"Address blacklisted: {EMAIL}"}},
                       "SendEmail")


def test_a_provider_error_becomes_its_class_and_code():
    wrapped = EmailDeliveryException("SES send_email failed", _ses_error())
    text = safe_error(wrapped)
    assert text == "EmailDeliveryException <- ClientError (MessageRejected)"
    assert EMAIL not in text


def test_a_validation_error_never_quotes_its_input():
    class Model(BaseModel):
        years: int
    with pytest.raises(ValidationError) as caught:
        Model(years="Jane Example, 12 Baker Street")
    assert safe_error(caught.value) == "ValidationError"


def test_the_ses_client_keeps_the_recipient_out_of_its_message_and_log(caplog):
    from app.services.notifications import ses_email_client as mod
    client = mod.SESEmailClient()
    with patch.object(mod, "email_delivery_is_log", return_value=False), \
            patch.object(client, "_get_client", return_value=MagicMock(send_email=MagicMock(side_effect=_ses_error()))):
        with pytest.raises(EmailDeliveryException) as caught:
            client.send_email(to_address=EMAIL, subject="Hi Jane", body_text="b")
    assert EMAIL not in str(caught.value) and caught.value.original is not None
    with patch.object(mod, "email_delivery_is_log", return_value=True), caplog.at_level(logging.WARNING):
        client.send_email(to_address=EMAIL, subject="Interview for Jane Example", body_text="b")
    assert "EMAIL NOT SENT" in caplog.text and EMAIL not in caplog.text and "Jane" not in caplog.text


def test_storage_errors_name_no_file():
    from app.core.storage_service import _key_ref
    assert _key_ref("campaign_1/bulk-zip/j/0003_Jane_Example_CV.pdf") == "campaign_1/bulk-zip/j/<file>"
    assert _key_ref("campaign_1/bulk-zip/j/0003.pdf") == "campaign_1/bulk-zip/j/0003.pdf"
    assert _key_ref("org_x/resume/3f2a9c1e-0000-4000-8000-000000000000.pdf").endswith(".pdf")


def test_bulk_files_are_staged_under_their_position_only():
    from app.services.resume.bulk_zip_entries import staged_entry_index, staged_file_name
    assert staged_file_name(3, "Jane Example CV.PDF") == "0003.pdf"
    assert staged_file_name(12, "no_extension") == "0012"
    assert staged_entry_index("campaign_x/bulk-zip/j/0003.pdf") == 3
    assert staged_entry_index("campaign_x/bulk-zip/j/0012") == 12
    assert staged_entry_index("campaign_x/bulk-zip/j/0003_Jane_CV.pdf") == 3          # staged before


def test_database_errors_hide_their_parameters():
    from app.db.database import engine
    assert engine.hide_parameters is True


def test_a_reparse_dead_letter_payload_leaves_the_reason_out():
    from app.tasks.resume_processing_tasks import replay_payload
    payload = replay_payload("r", "p", {"version_id": "v", "reason": "Jane's CV was cut off", "actor_id": "u"})
    assert payload["reparse"] == {"version_id": "v", "actor_id": "u"}


def test_the_retry_exception_carries_no_message_text():
    assert str(SafeRetryError(safe_error(_ses_error()))) == "ClientError (MessageRejected)"


# The sinks the review named: no str(exc) goes into a task log, dead letter or stage row any more.
SINK_CALLS = {"mark_failure", "mark_dead", "mark_retry", "record"}
SINK_KWARGS = {"final_error_message", "error_message", "error_reason", "error_summary", "summary"}
SCANNED = ["tasks", "services/document_processing", "services/notifications"]


def _str_of_exception(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"
            and len(node.args) == 1 and isinstance(node.args[0], (ast.Name, ast.Attribute))
            and (getattr(node.args[0], "id", None) or getattr(node.args[0], "attr", "")) in
            {"ex", "exc", "e", "error", "embed_ex", "original", "last_error"})


def test_no_exception_text_reaches_a_stored_sink():
    offenders = []
    for folder in SCANNED:
        for path in (APP / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in SINK_CALLS:
                    if any(_str_of_exception(a) for a in node.args):
                        offenders.append(f"{path.name}:{node.lineno}")
                    if any(k.arg in SINK_KWARGS and _str_of_exception(k.value) for k in node.keywords):
                        offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Assign) and _str_of_exception(node.value):
                    names = [getattr(t, "id", getattr(t, "attr", "")) for t in node.targets]
                    if any(n in SINK_KWARGS for n in names):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
