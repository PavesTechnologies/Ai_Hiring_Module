from typing import Generator
from sqlalchemy.orm import Session, sessionmaker
from DataAccess_Layer.utils.db_connection import engine

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
