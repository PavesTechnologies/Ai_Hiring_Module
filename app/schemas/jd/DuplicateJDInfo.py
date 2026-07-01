from pydantic import BaseModel
from uuid import UUID
from datetime import datetime


class ExistingJDInfo(BaseModel):
    id: UUID
    title: str
    version_number: int
    created_at: datetime
    
class DuplicateJDInfo(BaseModel):
    message: str
    existing_jd: ExistingJDInfo
    actions:list[str]