from fastapi import APIRouter, Depends, Request

from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles

router = APIRouter(prefix="/test", tags=["RBAC Tests"])



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


