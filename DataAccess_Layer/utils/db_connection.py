import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_hiring")

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass
