"""The Redis cache prefix is derived from the connected database: two databases never share cached data."""
import pytest

from app.core import cache_keys
from app.core.config import settings


def _with(**db):
    return settings.model_copy(update=db)


def test_two_databases_get_two_cache_namespaces_and_one_database_always_the_same():
    dev = _with(db_host="dev-db.example.internal", db_port="5432", db_name="airs")
    fresh = _with(db_host="127.0.0.1", db_port="45433", db_name="airs_fresh")
    other_name = _with(db_host="dev-db.example.internal", db_port="5432", db_name="airs_copy")
    assert len({dev.cache_namespace, fresh.cache_namespace, other_name.cache_namespace}) == 3
    assert dev.cache_namespace == _with(db_host="DEV-DB.example.internal ", db_port="5432",
                                        db_name="airs").cache_namespace          # same database, same namespace
    assert dev.cache_namespace.startswith(f"{settings.cache_key_prefix}:")


def test_the_namespace_never_carries_the_database_credentials_or_host():
    db = _with(db_host="secret-host.example", db_user="admin_user", db_password="pw-123", db_name="airs")
    assert not any(part in db.cache_namespace for part in ("secret-host", "admin_user", "pw-123"))


@pytest.mark.parametrize("key", [
    lambda: cache_keys.skill_catalog_key(),
    lambda: cache_keys.jd_key("00000000-0000-0000-0000-000000000001"),
    lambda: cache_keys.campaign_key("00000000-0000-0000-0000-000000000001"),
])
def test_every_cache_key_is_in_the_connected_databases_namespace(key):
    assert key().startswith(f"{settings.cache_namespace}:")
