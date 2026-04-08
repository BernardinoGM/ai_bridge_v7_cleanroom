from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    user_id: int | None = Field(default=None, description="AI Bridge internal user id")
    mode: Literal["fast", "smart", "assured"] = "smart"
    messages: list[ChatMessage]
    stream: bool = False


class MessagesRequest(BaseModel):
    user_id: int | None = None
    mode: Literal["fast", "smart", "assured"] = "smart"
    system: str | None = None
    messages: list[dict[str, Any]]
    task_id: str | None = None
    task_action: Literal["continue", "escalate", "deescalate"] | None = None
    source_surface: str | None = None
    stream: bool = False


class CheckoutCreateRequest(BaseModel):
    user_id: int | None = None
    email: str | None = None
    pack_code: Literal["starter", "growth", "scale", "scale_plus", "volume"]
    referred_by_code: str | None = None


class DemoChatRequest(BaseModel):
    example: Literal["spec", "refactor", "reply"]


class ApiKeyCreateRequest(BaseModel):
    email: str
    use_case: str | None = None
    referred_by_code: str | None = None
