"""
A guard for the test-data seeds (seed_users, seed_m12_test_campaign, import_aiven_hiring_manager):
they hold a fake admin login, a real employee's details and synthetic candidates, so they must
never run against production. Each calls refuse_unless_seed_data_allowed() before it opens a
database session.

A test seed runs only with an explicit ALLOW_TEST_SEEDS=1 in its environment, and never when
APP_ENV says production - an unset APP_ENV alone can't let it through.
"""
import os

from app.core.config import settings

PRODUCTION_ENVS = {"production", "prod"}


def seed_data_allowed() -> tuple[bool, str]:
    if (settings.app_env or "").strip().lower() in PRODUCTION_ENVS:
        return False, f"APP_ENV is {settings.app_env!r}"
    if os.environ.get("ALLOW_TEST_SEEDS", "").strip() != "1":
        return False, "ALLOW_TEST_SEEDS=1 is not set"
    return True, ""


def refuse_unless_seed_data_allowed(seed_name: str) -> None:
    allowed, reason = seed_data_allowed()
    if not allowed:
        raise SystemExit(f"REFUSING TO RUN {seed_name}: test-data seed, never for production ({reason})")
