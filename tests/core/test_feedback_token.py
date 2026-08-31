import time
from uuid import uuid4

import pytest

from app.core import feedback_token
from app.core.config import settings


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(settings, "feedback_token_signing_key", "test-feedback-signing-key")


def test_sign_then_verify_round_trips_both_ids():
    interview_schedule_id = uuid4()
    interviewer_id = uuid4()
    token = feedback_token.sign_feedback_token(interview_schedule_id, interviewer_id)

    result_schedule_id, result_interviewer_id = feedback_token.verify_feedback_token(token)

    assert result_schedule_id == interview_schedule_id
    assert result_interviewer_id == interviewer_id


def test_verify_rejects_a_tampered_payload():
    token = feedback_token.sign_feedback_token(uuid4(), uuid4())
    payload_b64, signature = token.split(".", 1)
    tampered = f"{payload_b64}x.{signature}"

    with pytest.raises(ValueError):
        feedback_token.verify_feedback_token(tampered)


def test_verify_rejects_a_forged_signature():
    token = feedback_token.sign_feedback_token(uuid4(), uuid4())
    payload_b64, _ = token.split(".", 1)
    forged = f"{payload_b64}.not-the-real-signature"

    with pytest.raises(ValueError):
        feedback_token.verify_feedback_token(forged)


def test_verify_rejects_a_malformed_token_with_no_separator():
    with pytest.raises(ValueError):
        feedback_token.verify_feedback_token("not-a-valid-token-at-all")


def test_verify_rejects_an_expired_token(monkeypatch):
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 1_300_000)  # ~15 days ago
    token = feedback_token.sign_feedback_token(uuid4(), uuid4())
    monkeypatch.setattr(time, "time", real_time)

    with pytest.raises(ValueError, match="expired"):
        feedback_token.verify_feedback_token(token)


def test_default_ttl_is_fourteen_days_not_oauths_ten_minutes():
    assert feedback_token._DEFAULT_TTL_SECONDS == 14 * 24 * 60 * 60


def test_two_interviewers_on_the_same_round_get_distinct_tokens():
    interview_schedule_id = uuid4()
    interviewer_a, interviewer_b = uuid4(), uuid4()

    token_a = feedback_token.sign_feedback_token(interview_schedule_id, interviewer_a)
    token_b = feedback_token.sign_feedback_token(interview_schedule_id, interviewer_b)

    assert token_a != token_b
    assert feedback_token.verify_feedback_token(token_a) == (interview_schedule_id, interviewer_a)
    assert feedback_token.verify_feedback_token(token_b) == (interview_schedule_id, interviewer_b)
