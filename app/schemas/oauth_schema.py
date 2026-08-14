from pydantic import BaseModel


class OAuthConnectResponse(BaseModel):
    auth_url: str


class OAuthStatusResponse(BaseModel):
    connected: bool
