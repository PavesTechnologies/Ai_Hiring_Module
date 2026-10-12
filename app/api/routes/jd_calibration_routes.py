"""JD calibration (Phase 2): everyone who works with JDs can see it; only HR_ADMIN can change it."""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.jd import get_jd_calibration_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.jd.calibration import (
    JDCalibrationSaveRequest,
    JDCalibrationSaveResponse,
    JDCalibrationVersionItem,
    JDCalibrationView,
)
from app.schemas.response import APIResponse
from app.services.jd.jd_calibration_service import JDCalibrationService

router = APIRouter(prefix="/job-descriptions", tags=["JD Calibration"])

_VIEWERS = (UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)


@router.get(
    "/{jd_id}/calibration",
    response_model=APIResponse[JDCalibrationView],
    status_code=status.HTTP_200_OK,
    summary="Get JD calibration",
    description="Calibration status (with unknown-skill verification as a detail), skills by tier, "
                "ambiguous-skill senses, experience and tolerance, education. Read-only for non-HR roles.",
)
def get_jd_calibration(
    jd_id: UUID,
    service: JDCalibrationService = Depends(get_jd_calibration_service),
    user: TokenUser = Security(require_roles(*_VIEWERS)),
):
    can_calibrate = UserRole.HR_ADMIN.value in user.roles
    return APIResponse.ok(data=service.get_view(jd_id, can_calibrate=can_calibrate))


@router.put(
    "/{jd_id}/calibration",
    response_model=APIResponse[JDCalibrationSaveResponse],
    status_code=status.HTTP_200_OK,
    summary="Calibrate a JD (HR_ADMIN)",
    description="Confirms or edits the JD's scoring inputs. Creates a new calibration version when "
                "anything changed; live campaigns' scored candidates are marked 'score out of date'.",
)
def save_jd_calibration(
    jd_id: UUID,
    request: JDCalibrationSaveRequest,
    service: JDCalibrationService = Depends(get_jd_calibration_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.save(jd_id, request, actor_id=user.user_id,
                          actor_role=user.roles[0] if user.roles else None)
    message = ("JD calibrated." if result.changed else "No changes - calibration unchanged.")
    return APIResponse.ok(data=result, message=message)


@router.get(
    "/{jd_id}/calibration/versions",
    response_model=APIResponse[list[JDCalibrationVersionItem]],
    status_code=status.HTTP_200_OK,
    summary="JD calibration history",
)
def list_jd_calibration_versions(
    jd_id: UUID,
    service: JDCalibrationService = Depends(get_jd_calibration_service),
    user: TokenUser = Security(require_roles(*_VIEWERS)),
):
    return APIResponse.ok(data=service.list_versions(jd_id))
