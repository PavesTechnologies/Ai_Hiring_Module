"""ConfigRepository.update_description against real Postgres (rolled back): only the description changes."""
import uuid

from app.models.config import PlatformConfig
from app.repositories.config_repository import ConfigRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def test_a_settings_description_changes_and_its_value_does_not(db):  # noqa: F811
    key = f"TEST_DESCRIPTION_{uuid.uuid4().hex[:8].upper()}"
    db.add(PlatformConfig(id=uuid.uuid4(), key=key, value="20", description="old wording"))
    db.flush()
    repo = ConfigRepository(db)
    assert repo.update_description(key, "current wording", None) == ("old wording", "current wording")
    db.expire_all()
    row = db.query(PlatformConfig).filter(PlatformConfig.key == key).one()
    assert (row.value, row.description) == ("20", "current wording")
    assert repo.update_description("NO_SUCH_SETTING_KEY", "x", None) is None
