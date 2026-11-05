"""The test-data seeds refuse to run unless ALLOW_TEST_SEEDS=1, and never with APP_ENV=production."""
import importlib
import sys

import pytest

from app.core.config import settings
from app.seeds import seed_data_guard as guard

TEST_SEEDS = ("seed_users", "seed_m12_test_campaign", "import_aiven_hiring_manager")


@pytest.mark.parametrize("app_env, flag, allowed", [
    ("development", "1", True),
    ("development", "", False),
    ("development", "yes", False),
    ("production", "1", False),
    (" PROD ", "1", False),
])
def test_allowed_only_with_the_flag_outside_production(monkeypatch, app_env, flag, allowed):
    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setenv("ALLOW_TEST_SEEDS", flag)
    assert guard.seed_data_allowed()[0] is allowed


@pytest.mark.parametrize("name", TEST_SEEDS)
def test_each_test_seed_refuses_before_it_opens_a_session(monkeypatch, name):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setenv("ALLOW_TEST_SEEDS", "1")
    sys.modules.pop(f"app.seeds.{name}", None)
    import app.db.session as session
    monkeypatch.setattr(session, "SessionLocal", lambda: pytest.fail("a session was opened"))
    with pytest.raises(SystemExit, match=f"REFUSING TO RUN {name}"):
        importlib.import_module(f"app.seeds.{name}")
    sys.modules.pop(f"app.seeds.{name}", None)
