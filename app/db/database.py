from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=3,
    pool_recycle=1800,
)


class Base(DeclarativeBase):
    pass


def check_connection() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        raise RuntimeError(f"Database connection failed: {exc}") from exc
