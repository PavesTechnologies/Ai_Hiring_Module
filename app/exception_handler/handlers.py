from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from app.schemas.response import APIResponse
from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.candidate_exceptions import CandidateErasureBlockedException
from app.exceptions.resume_exceptions import ResumeException
from app.exceptions.storage_exception import StorageException


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


async def resume_exception_handler(
    request: Request,
    exc: ResumeException,
):
    """
    Handle resume-upload-specific exceptions: unsupported file format, file
    size exceeded, corrupt/password-protected file, encryption service
    unavailable. One handler for the shared base class catches all four
    subclasses via FastAPI's exception-MRO lookup.
    """
    response = APIResponse.fail(
        message=exc.message,
        data=exc.data,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(),
    )


async def candidate_erasure_blocked_exception_handler(
    request: Request,
    exc: CandidateErasureBlockedException,
):
    """Handle uploads blocked because the candidate has an active erasure request."""
    response = APIResponse.fail(
        message=exc.message,
    )
    return JSONResponse(
        status_code=409,
        content=response.model_dump(),
    )


async def storage_exception_handler(
    request: Request,
    exc: StorageException,
):
    """
    Handle storage-backend failures (e.g. Supabase Storage unavailable).
    Deliberately returns a fixed, generic message rather than exc.message —
    the underlying detail (bucket name, path) is internal infrastructure
    detail that shouldn't be exposed to the client.
    """
    response = APIResponse.fail(
        message="File storage temporarily unavailable. Please try again in a few minutes.",
    )
    return JSONResponse(
        status_code=503,
        content=response.model_dump(),
    )
