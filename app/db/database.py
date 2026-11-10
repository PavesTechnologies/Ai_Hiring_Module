from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    # Review item 28: database errors (str(exc)) and echoed SQL carry no row values.
    hide_parameters=True,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=1800,
)


class Base(DeclarativeBase):
    pass

