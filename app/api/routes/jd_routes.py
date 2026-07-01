from uuid import UUID
from fastapi import APIRouter, Depends, status

from app.dependencies.jd import get_jd_service
from app.schemas.jd.request import CreateJDRequest
from app.schemas.jd.repondse import CreateJDResponse
from app.services.jd.jd_service import JDService

router = APIRouter(
    prefix="/job-descriptions",
    tags=["Job Descriptions"],
)


SYSTEM_USER = UUID("22222222-2222-2222-2222-222222222222")

@router.post(
    "",
    response_model=CreateJDResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job_description(
    request: CreateJDRequest,
    service: JDService = Depends(get_jd_service),
):
    return service.create_jd(
        request=request,
        created_by=SYSTEM_USER
    )