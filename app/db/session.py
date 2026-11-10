from typing import Generator

from sqlalchemy.orm import Session, sessionmaker

from app.db.database import engine

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def rollback_quietly(*sessions) -> None:
    """
    Review item 22: the first thing a task's failure handler does. After a database error the session
    is unusable until rolled back - the failure bookkeeping (task log, job status, dead letter) would
    raise on it, hide the real error and leave the row RUNNING. A rollback that itself fails is logged,
    never raised: it must not mask the error being handled.
    """
    import logging
    for session in sessions:
        try:
            session.rollback()
        except Exception:
            logging.getLogger(__name__).exception("Rollback in a task failure handler failed")
