from typing import Any, Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    success: bool
    message: str
    data: T | None = None

    @classmethod
    def ok(cls, data: Any = None, message: str = "Success") -> "APIResponse":
        return cls(success=True, message=message, data=data)

    @classmethod
    def fail(cls, message: str, data: Any = None) -> "APIResponse":
        return cls(success=False, message=message, data=data)
