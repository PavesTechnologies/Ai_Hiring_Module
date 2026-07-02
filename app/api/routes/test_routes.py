from fastapi import APIRouter, Depends, Request

from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles

router = APIRouter(prefix="/test", tags=["RBAC Tests"])


@router.get("/public")
async def public_endpoint():
    return {
        "endpoint": "public-test",
        "auth_required": False,
    }


@router.get("/me")
async def who_am_i(request: Request):
    payload = getattr(request.state, "token_payload", None)

    return {
        "token_payload": payload,
        "message": "Token is valid" if payload else "No token found",
    }


@router.get("/any-role")
async def any_authenticated(
    user: TokenUser = Depends(require_roles())
):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "roles": user.roles,
    }


@router.get("/hr-admin")
async def hr_admin_only(
    user: TokenUser = Depends(
        require_roles(UserRole.HR_ADMIN)
    )
):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "roles": user.roles,
    }


@router.get("/recruiter")
async def recruiter_only(
    user: TokenUser = Depends(
        require_roles(UserRole.RECRUITER)
    )
):
    return {
        "message": "Welcome, Recruiter",
        "user": user.email,
        "roles": user.roles,
    }


@router.get("/hiring-manager")
async def hiring_manager_only(
    user: TokenUser = Depends(
        require_roles(UserRole.HIRING_MANAGER)
    )
):
    return {
        "message": "Welcome, Hiring Manager",
        "user": user.email,
        "roles": user.roles,
    }


@router.get("/hr-or-manager")
async def hr_or_manager(
    user: TokenUser = Depends(
        require_roles(
            UserRole.HR_ADMIN,
            UserRole.HIRING_MANAGER,
        )
    )
):
    return {
        "message": "Access granted (HR Admin or Hiring Manager)",
        "user": user.email,
        "roles": user.roles,
    }