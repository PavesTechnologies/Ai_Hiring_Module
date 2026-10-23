"""JD calibration (Phase 2): everyone who works with JDs can see it; only HR_ADMIN can change it."""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.jd import get_jd_calibration_service
from app.dependencies.jd_unknown_skills import get_jd_calibration_skill_service, get_jd_unknown_skill_service
from app.middleware.rbac import TokenUser, authorising_role, require_roles
from app.models.identity import UserRole
from app.schemas.jd.calibration import (
    JDCalibrationSaveRequest,
    JDCalibrationSaveResponse,
    JDCalibrationVersionItem,
    JDCalibrationAddSkillRequest,
    JDCalibrationSkillChangeResponse,
    JDCalibrationView,
    JDUnknownSkillResolveRequest,
    JDUnknownSkillResolveResponse,
    UnknownSkillSuggestions,
)
from app.schemas.response import APIResponse
from app.services.jd.jd_calibration_service import JDCalibrationService
from app.services.jd.jd_calibration_skill_service import JDCalibrationSkillService
from app.services.jd.jd_unknown_skill_service import JDUnknownSkillService

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
                          actor_role=authorising_role(user))
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


# --- unknown skills (Phase 2 Step 2) -------------------------------------------

@router.get(
    "/{jd_id}/unknown-skills/suggestions",
    response_model=APIResponse[list[UnknownSkillSuggestions]],
    status_code=status.HTTP_200_OK,
    summary="Top-3 catalogue suggestions for each unresolved unknown skill of a JD",
)
def get_jd_unknown_skill_suggestions(
    jd_id: UUID,
    service: JDUnknownSkillService = Depends(get_jd_unknown_skill_service),
    user: TokenUser = Security(require_roles(*_VIEWERS)),
):
    return APIResponse.ok(data=service.suggestions(jd_id))


@router.post(
    "/{jd_id}/unknown-skills/{unknown_skill_id}/resolve",
    response_model=APIResponse[JDUnknownSkillResolveResponse],
    status_code=status.HTTP_200_OK,
    summary="Map / approve as new / drop an unknown skill (HR_ADMIN)",
    description="MAP and DROP apply to this JD only; NEW adds the skill to the catalogue and resolves every "
                "JD with this text. Each JD resolved: verification recomputed, a calibration version if "
                "calibrated, live campaigns' scored candidates marked 'score out of date'.",
)
def resolve_jd_unknown_skill(
    jd_id: UUID,
    unknown_skill_id: UUID,
    request: JDUnknownSkillResolveRequest,
    service: JDUnknownSkillService = Depends(get_jd_unknown_skill_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.resolve(jd_id, unknown_skill_id, request, actor_id=user.user_id,
                             actor_role=authorising_role(user))
    return APIResponse.ok(data=result, message={"MAP": "Mapped.", "NEW": "Added to the catalogue.",
                                                "DROP": "Dropped from this JD."}[request.action])


# --- add / remove skills in calibration ------------------------------------------

@router.post(
    "/{jd_id}/calibration/skills",
    response_model=APIResponse[JDCalibrationSkillChangeResponse],
    status_code=status.HTTP_200_OK,
    summary="Add a skill the extraction missed (HR_ADMIN)",
    description="A catalogue skill (canonical_skill_id) or a new one (new_skill_name, added to the catalogue), "
                "with a tier. Recorded as an HR decision; a calibrated JD saves a new calibration version and "
                "live campaigns' scored candidates are marked 'score out of date'.",
)
def add_jd_calibration_skill(
    jd_id: UUID,
    request: JDCalibrationAddSkillRequest,
    service: JDCalibrationSkillService = Depends(get_jd_calibration_skill_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.add(jd_id, request, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(data=result, message=f"'{result.skill}' added.")


@router.delete(
    "/{jd_id}/calibration/skills/{jd_skill_id}",
    response_model=APIResponse[JDCalibrationSkillChangeResponse],
    status_code=status.HTTP_200_OK,
    summary="Remove a skill from the JD, e.g. one the extraction invented (HR_ADMIN)",
)
def remove_jd_calibration_skill(
    jd_id: UUID,
    jd_skill_id: UUID,
    service: JDCalibrationSkillService = Depends(get_jd_calibration_skill_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.remove(jd_id, jd_skill_id, actor_id=user.user_id,
                            actor_role=authorising_role(user))
    return APIResponse.ok(data=result, message=f"'{result.skill}' removed from this JD.")
