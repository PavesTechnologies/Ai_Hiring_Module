from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from app.schemas.response import APIResponse
from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.exceptions.campaign_exceptions import CampaignException


async def duplicate_jd_exception_handler(
    request: Request,
    exc: DuplicateJDException,
):
    """Handle duplicate JD exceptions."""
    response = APIResponse.fail(
        message="Duplicate Job Description found",
        data={
            "existing_jd_id": str(exc.existing_jd.id),
            "title": exc.existing_jd.title,
            "version_number": exc.existing_jd.version_number,
        }
    )
    return JSONResponse(
        status_code=409,
        content=response.model_dump(),
    )


async def campaign_exception_handler(
    request: Request,
    exc: CampaignException,
):
    """Handle all campaign-related exceptions."""
    response = APIResponse.fail(
        message=exc.message,
        data=exc.data
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(),
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
):
    """Handle all HTTP exceptions with standardized APIResponse format."""
    response = APIResponse.fail(
        message=exc.detail,
        data=None
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(),
    )
