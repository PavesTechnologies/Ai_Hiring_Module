from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.jd_repository import JDRepository
from app.services.jd.jd_service import JDService
from app.services.jd.hash_service import HashService


def get_jd_repository(
    db: Session = Depends(get_db),
)-> JDRepository:
    return JDRepository(db)

def get_hash_service() -> HashService:
    return HashService()


def get_jd_service(
    repository: JDRepository = Depends(get_jd_repository),
    hash_service: HashService = Depends(get_hash_service),
)-> JDService:
    return JDService(
        repository=repository,
        hash_service=hash_service,
    )
