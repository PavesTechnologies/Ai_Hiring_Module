import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# ── project root on sys.path ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

# ── Alembic config ────────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Build DATABASE_URL from individual .env variables ─────────────────────────
_driver   = os.getenv("DB_DRIVER",  "postgresql+psycopg2")
_user     = os.getenv("DB_USER")
_password = os.getenv("DB_PASSWORD")
_host     = os.getenv("DB_HOST")
_port     = os.getenv("DB_PORT", "5432")
_name     = os.getenv("DB_NAME")
_sslmode  = os.getenv("DB_SSLMODE", "require")

config.set_main_option(
    "sqlalchemy.url",
    f"{_driver}://{_user}:{_password}@{_host}:{_port}/{_name}?sslmode={_sslmode}",
)

# ── Import every model module so SQLAlchemy registers them with Base.metadata ─
from app.db.database import Base  # noqa: E402

import app.models.identity          # noqa: F401, E402
import app.models.config            # noqa: F401, E402
import app.models.jd.job_descriptions  # noqa: F401, E402
import app.models.campaigns         # noqa: F401, E402
import app.models.candidates        # noqa: F401, E402
import app.models.embeddings        # noqa: F401, E402
import app.models.pipeline          # noqa: F401, E402
import app.models.skills            # noqa: F401, E402
import app.models.ai_pipeline       # noqa: F401, E402
import app.models.compliance        # noqa: F401, E402
import app.models.async_tasks       # noqa: F401, E402
import app.models.search            # noqa: F401, E402
import app.models.campaign_weight_preset  # noqa: F401, E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
