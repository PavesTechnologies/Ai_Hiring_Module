from uuid import UUID

from pydantic import BaseModel


class CreateJDResponse(BaseModel):

    id: UUID

    title: str

    version_number: int

    source_format: str

    jurisdiction: str