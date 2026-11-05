"""The system organisation base seed: creates the row POST /campaigns and the JD upload need, once."""
from unittest.mock import MagicMock

from app.api.routes import campaign_routes, jd_routes
from app.models.identity import SYSTEM_ORG_ID, Organization
from app.seeds import seed_system_organization as seed


def test_creates_the_row_on_a_fresh_database():
    db = MagicMock()
    db.get.return_value = None
    assert seed.seed(db) is True
    added = db.add.call_args.args[0]
    assert isinstance(added, Organization) and added.id == SYSTEM_ORG_ID and added.name
    db.commit.assert_called_once()


def test_leaves_an_existing_row_as_it_is():
    db = MagicMock()
    db.get.return_value = Organization(id=SYSTEM_ORG_ID, name="Renamed")
    assert seed.seed(db) is False
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_the_routes_write_the_seeded_id():
    assert campaign_routes.SYSTEM_ORG == jd_routes.SYSTEM_ORG == SYSTEM_ORG_ID
