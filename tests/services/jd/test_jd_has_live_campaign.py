"""JD details report a live campaign (skill edits then go through calibration), uncached."""
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.jd.jd_service import JDService


def _service(cache):
    repository = MagicMock()
    service = JDService(repository, MagicMock(), MagicMock(), MagicMock(), MagicMock())
    service.cache_service = cache
    return service, repository


def _response(jd_id):
    response = MagicMock(id=jd_id, has_live_campaign=False)
    return response


def test_jd_details_say_whether_a_campaign_using_the_jd_is_live():
    jd_id = uuid4()
    service, repository = _service(cache=None)
    service._load_jd_response = MagicMock(return_value=_response(jd_id))

    repository.has_active_campaign.return_value = True
    assert service.get_by_id(str(jd_id)).has_live_campaign is True
    repository.has_active_campaign.assert_called_with(jd_id)

    repository.has_active_campaign.return_value = False
    assert service.get_by_id(str(jd_id)).has_live_campaign is False
