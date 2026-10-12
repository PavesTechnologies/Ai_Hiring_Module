"""
A campaign can't be active unless its JD is calibrated (create, PAUSED -> ACTIVE,
reopen). Campaigns that existed before calibration are exempt - they keep working
with a "JD not calibrated" warning - until their JD is calibrated.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus
from app.models.jd.job_descriptions import JDCalibrationStatus, JDVerificationStatus
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest
from app.services.campaign.campaign_service import (
    CampaignService, jd_calibration_fields, jd_not_calibrated_message,
)


def _jd(calibrated):
    return SimpleNamespace(
        id=uuid4(), title="AI transformation lead", is_active_version=True, closed_at=None,
        calibration_status=JDCalibrationStatus.CALIBRATED if calibrated else JDCalibrationStatus.NOT_CALIBRATED,
        is_verified=JDVerificationStatus.VERIFIED, version_number=1, jurisdiction=None,
    )


def _service(jd, campaign=None):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_by_id_for_update.return_value = campaign
    jd_repo = MagicMock()
    jd_repo.get_by_id.return_value = jd
    service = CampaignService(campaign_repo, jd_repo, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                              circuit_breaker_repo=MagicMock(), dead_letter_queue_repo=MagicMock(),
                              prompt_template_repo=MagicMock())
    return service, campaign_repo


def _create_request(jd):
    return CampaignCreateRequest(
        name="New campaign", jd_id=jd.id, weight_deterministic=Decimal("30"), weight_semantic=Decimal("40"),
        weight_ai=Decimal("30"), semantic_threshold=Decimal("0.65"), ai_threshold=Decimal("50"),
        prompt_template_id=uuid4(), ai_evaluate_prompt_id=uuid4(), hiring_manager_id="hm-1", recruiter_id="rec-1")


def _campaign(status, exempt):
    return SimpleNamespace(id=uuid4(), name="Old campaign", jd_id=uuid4(), status=status,
                           jd_calibration_exempt=exempt, max_candidates=None, deadline=None,
                           updated_at=datetime.now(timezone.utc))


# --- create ------------------------------------------------------------------

def test_creating_a_campaign_on_an_uncalibrated_jd_is_blocked():
    jd = _jd(calibrated=False)
    service, campaign_repo = _service(jd)

    with pytest.raises(CampaignException) as raised:
        service.create_campaign(_create_request(jd), org_id=uuid4(), created_by="hr-1")

    assert raised.value.status_code == 409
    assert raised.value.data["code"] == "JD_NOT_CALIBRATED"
    assert "not calibrated" in raised.value.message
    campaign_repo.create.assert_not_called()


def test_a_calibrated_jd_passes_the_gate():
    jd = _jd(calibrated=True)
    service, campaign_repo = _service(jd)
    campaign_repo.get_by_name.return_value = SimpleNamespace(name="New campaign")   # next check fails instead

    with pytest.raises(CampaignException) as raised:
        service.create_campaign(_create_request(jd), org_id=uuid4(), created_by="hr-1")

    assert "already exists" in raised.value.message                 # got past the calibration gate


# --- resume (PAUSED -> ACTIVE) ---------------------------------------------------

def test_resuming_a_non_exempt_campaign_on_an_uncalibrated_jd_is_blocked():
    campaign = _campaign(CampaignStatus.PAUSED, exempt=False)
    service, _ = _service(_jd(calibrated=False), campaign)

    with pytest.raises(CampaignException) as raised:
        service.update_campaign(campaign.id, CampaignUpdateRequest(status=CampaignStatus.ACTIVE), updated_by="hr-1")

    assert raised.value.status_code == 409 and raised.value.data["code"] == "JD_NOT_CALIBRATED"
    assert campaign.status == CampaignStatus.PAUSED


def test_an_exempt_campaign_can_still_resume_on_an_uncalibrated_jd():
    campaign = _campaign(CampaignStatus.PAUSED, exempt=True)
    assert jd_not_calibrated_message(_jd(calibrated=False), campaign) is None


# --- reopen ------------------------------------------------------------------------

def test_reopen_readiness_reports_an_uncalibrated_jd_unless_exempt():
    jd = _jd(calibrated=False)
    closed = _campaign(CampaignStatus.CLOSED, exempt=False)
    service, campaign_repo = _service(jd, closed)
    for counter in ("get_mandatory_unverified_skill_count", "get_unresolved_unknown_skill_count",
                    "get_candidate_count", "get_selected_count"):
        getattr(campaign_repo, counter).return_value = 0
    closed.weight_deterministic = closed.weight_semantic = closed.weight_ai = Decimal("33.33")

    readiness = service.get_reopen_readiness(closed.id)
    assert readiness.is_ready is False
    assert [i.code for i in readiness.issues] == ["JD_NOT_CALIBRATED"]

    closed.jd_calibration_exempt = True
    assert service.get_reopen_readiness(closed.id).is_ready is True


# --- status fields (badge + warning) ------------------------------------------------

def test_calibration_fields_show_the_status_with_verification_as_a_detail():
    fields = jd_calibration_fields(_jd(calibrated=True), _campaign(CampaignStatus.ACTIVE, exempt=False))
    assert fields == {"jd_calibration_status": "CALIBRATED", "jd_verification_status": "VERIFIED",
                      "jd_calibration_warning": None}


def test_an_exempt_campaign_on_an_uncalibrated_jd_shows_a_warning():
    fields = jd_calibration_fields(_jd(calibrated=False), _campaign(CampaignStatus.ACTIVE, exempt=True))
    assert fields["jd_calibration_status"] == "NOT_CALIBRATED"
    assert fields["jd_calibration_warning"].startswith("JD not calibrated")
